"""
Controlled live submission module.

Handles pre-submission validation, snapshots, browser submission,
confirmation detection, and live run abort on failure.
"""

import json, re, time
from typing import Any, Callable
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

from hackathon_searcher.database import (
    get_event_by_id, get_application, update_application,
    update_event, log_audit, has_application, insert_application,
    get_all_events, get_all_applications, get_applications_for_event,
    create_application_group,
)
from hackathon_searcher.forms import (
    extract_form_fields, generate_answer_for_field, validate_application,
    validate_factual_consistency, detect_form_provider, consent_policy_for_field,
)
from hackathon_searcher.models import FormField
from hackathon_searcher.browser import BrowserSession
from hackathon_searcher.profile import profile_manager
from hackathon_searcher.settings import settings
from hackathon_searcher.scoring import score_applicant_fit
from hackathon_searcher.auth import mark_auth_expired
from hackathon_searcher.team import load_team

CONFIRMATION_PHRASES = [
    "thank you for applying", "application received", "registration complete",
    "application submitted", "you're registered", "we've received your application",
    "application confirmed", "successfully registered", "your application has been",
    "thanks for applying", "submission received", "you are registered",
    "registration confirmed",
]

APPLICATION_LINK_PATTERNS = re.compile(
    r"apply|register|registration|sign.?up|join|participate|application|submit",
    re.IGNORECASE,
)
DISCOVERY_ACTION_PATTERNS = re.compile(
    r"apply(?:\s+now)?|register(?:\s+now)?|join(?:\s+us)?|sign\s*up|request\s+to\s+join|"
    r"attend|participate|become\s+a\s+hacker|hacker\s+application|applications?|"
    r"get\s+(?:tickets?|involved)|reserve\s+(?:a\s+)?spot|join\s+waitlist",
    re.IGNORECASE,
)
CLOSED_PATTERNS = re.compile(
    r"registration\s+(is\s+)?closed|applications?\s+(is\s+)?closed|no longer accepting|deadline has passed",
    re.IGNORECASE,
)
WAITLIST_PATTERNS = re.compile(r"waitlist|join the waitlist|sold out", re.IGNORECASE)
NOT_OPEN_PATTERNS = re.compile(
    r"applications?\s+(?:will\s+)?open|registration\s+(?:will\s+)?open|coming soon|not yet open|opens? later",
    re.IGNORECASE,
)
EMAIL_VERIFICATION_PATTERNS = re.compile(
    r"check (?:your )?(?:email|inbox)|verification code|one[- ]time code|enter (?:the )?code|"
    r"we (?:sent|have sent) (?:you )?(?:an )?(?:email|code)",
    re.IGNORECASE,
)
READY_REQUIRED_FIELDS = (
    "event_id", "applicant_id", "application_url", "form_provider",
    "eligibility_status", "event_score", "applicant_fit_score", "questions",
    "answers", "fact_check_passed", "cross_profile_check_passed",
    "duplicate_check_passed", "form_fillable", "application_open",
    "form_validation_status",
)
LIVE_ELIGIBLE_STATES = {"ELIGIBLE", "ELIGIBLE_CONFIRMED", "ELIGIBLE_NO_EXCLUSION_FOUND"}
TEAM_READY_STATUS = "TEAM_READY_TO_APPLY"
LIVE_CLICK_TIMEOUT_MS = 10_000
LIVE_CONFIRMATION_TIMEOUT_MS = 10_000
UNCERTAIN_SUBMISSION_STATUSES = {"SUBMISSION_STATUS_UNKNOWN", "SUBMITTED_CONFIRMATION_UNKNOWN"}


def _team_blocked_status(applications: dict[str, dict]) -> str:
    """Map a failing individual hard gate to the event-level team state."""
    statuses = " ".join(str(app.get("status", "")) for app in applications.values())
    if any(str(app.get("status", "")) in {"APPLIED", *UNCERTAIN_SUBMISSION_STATUSES} for app in applications.values()):
        return "TEAM_ALREADY_APPLIED"
    if "ELIGIB" in statuses or "INELIGIBLE" in statuses:
        return "TEAM_BLOCKED_ELIGIBILITY"
    if "SCHEDULE" in statuses:
        return "TEAM_BLOCKED_SCHEDULE"
    if "AUTH" in statuses or "LOGIN" in statuses:
        return "TEAM_BLOCKED_AUTH"
    if "UNKNOWN" in statuses or "UNREADABLE" in statuses:
        return "TEAM_BLOCKED_UNKNOWN_FIELD"
    return "TEAM_BLOCKED_FORM"


def _finalize_team_decision(event: dict, travel_status: str) -> list[dict[str, Any]]:
    """Persist one configurable team decision and return its ready entries."""
    team = load_team()
    member_ids = team.members
    applications = {app["applicant_id"]: app for app in get_applications_for_event(event["event_id"])}
    if any(member_id not in applications for member_id in member_ids):
        update_event(event["event_id"], {"team_status": "TEAM_BLOCKED_FORM"})
        return []
    scores = {aid: _safe_float(applications[aid].get("apply_score")) for aid in member_ids}
    team_score = round(sum(scores.values()) / len(scores), 2)
    eligible = all(applications[aid].get("eligibility_status") in LIVE_ELIGIBLE_STATES for aid in member_ids)
    hard_ready = all(
        applications[aid].get("status") == "TEAM_PENDING"
        and bool(applications[aid].get("application_open"))
        and bool(applications[aid].get("form_fillable"))
        and bool(applications[aid].get("fact_check_passed"))
        and bool(applications[aid].get("cross_profile_check_passed"))
        and bool(applications[aid].get("duplicate_check_passed"))
        and bool(applications[aid].get("consent_policy_passed"))
        and applications[aid].get("form_validation_status") == "VALIDATED"
        and int(applications[aid].get("unreadable_required_fields") or 0) == 0
        for aid in member_ids
    )
    travel_ok = {aid: bool(applications[aid].get("travel_eligible")) for aid in member_ids}
    official_travel = travel_status in {"CONFIRMED_FLIGHTS", "CONFIRMED_TRAVEL_REIMBURSEMENT", "CONFIRMED_TRAVEL_STIPEND"}
    team_travel_status = travel_status if official_travel and all(travel_ok.values()) else "NOT_CONFIRMED_FOR_TEAM"
    event_updates = {
        "team_member_scores": json.dumps(scores), "team_member_travel_eligibility": json.dumps(travel_ok),
        "team_apply_score": team_score, "team_travel_status": team_travel_status,
    }
    if event.get("status") == "SCHEDULE_CONFLICT":
        event_updates["team_status"] = "TEAM_BLOCKED_SCHEDULE"
        update_event(event["event_id"], event_updates)
        for aid in member_ids:
            _update_blocked_application(event["event_id"], aid, "TEAM_BLOCKED_SCHEDULE", "Team schedule conflict")
        return []
    if not eligible or not hard_ready:
        event_updates["team_status"] = _team_blocked_status(applications)
        update_event(event["event_id"], event_updates)
        return []
    if team_score < team.minimum_team_score:
        event_updates["team_status"] = "TEAM_BELOW_THRESHOLD"
        update_event(event["event_id"], event_updates)
        for aid in member_ids:
            _update_blocked_application(event["event_id"], aid, "TEAM_BELOW_THRESHOLD", f"Team apply score {team_score:.2f} below {team.minimum_team_score}")
        return []
    group_id = f"team_{event['event_id'][:12]}_{team.team_id}"
    create_application_group(group_id, event["event_id"], list(member_ids))
    event_updates.update({"team_status": TEAM_READY_STATUS, "team_application_group_id": group_id})
    update_event(event["event_id"], event_updates)
    ready: list[dict[str, Any]] = []
    for aid in member_ids:
        app = applications[aid]
        update_application(event["event_id"], aid, {"status": "READY_TO_APPLY", "application_group_id": group_id, "notes": f"{TEAM_READY_STATUS}; team_apply_score={team_score:.2f}"})
        profile = profile_manager.get(aid)
        ready.append({"event_id": event["event_id"], "event_name": event.get("event_name", ""), "applicant_id": aid,
                      "applicant_name": profile.name if profile else aid, "event_score": _safe_float(event.get("event_score")),
                      "fit_score": _safe_float(app.get("applicant_fit_score")), "apply_score": scores[aid],
                      "team_apply_score": team_score, "travel_support": travel_status,
                      "application_url": app.get("application_url", ""), "form_provider": app.get("form_provider", ""),
                      "team_decision": TEAM_READY_STATUS, "reason": "Both team members passed hard gates and the team threshold"})
    return ready


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def _json_value(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value if value is not None else default


def _has_json_content(value: Any) -> bool:
    parsed = _json_value(value, value)
    return bool(parsed)


def _cross_profile_issues(applicant_id: str, answers: list[dict]) -> list[str]:
    forbidden: list[str] = []
    for other in profile_manager.all_profiles:
        if other.applicant_id == applicant_id:
            continue
        forbidden.extend(value.lower() for value in (other.full_name, other.email) if value)
    issues = []
    for item in answers:
        answer = str(item.get("answer", ""))
        for word in forbidden:
            if word in answer.lower():
                issues.append(f"Cross-profile contamination: {word}")
    return sorted(set(issues))


def _form_is_actionable(browser: BrowserSession) -> bool:
    for selector in (
        "button", "input[type='submit']", "[role='button']",
    ):
        for element in browser.page.query_selector_all(selector):
            text = " ".join(filter(None, [
                element.inner_text() if selector != "input[type='submit']" else "",
                element.get_attribute("value"),
                element.get_attribute("aria-label"),
            ]))
            if APPLICATION_LINK_PATTERNS.search(text):
                return True
    return False


def _is_luma_url(url: str) -> bool:
    return "luma.com" in urlparse(url).netloc.lower()


def _has_public_email_gate(fields: list[FormField], actionable: bool) -> bool:
    """Return true for an email-first public registration step, not account auth."""
    if not actionable or not fields:
        return False
    email_only = all(
        field.field_type in {"email", "hidden"} or "email" in f"{field.label} {field.name}".lower()
        for field in fields
    )
    return email_only and any(
        field.field_type == "email" or "email" in f"{field.label} {field.name}".lower()
        for field in fields
    ) and not any("password" in f"{field.label} {field.name}".lower() for field in fields)


def _advance_public_email_gate(browser: BrowserSession, email: str) -> bool:
    """Advance a public email-first application step without clicking final Submit."""
    try:
        email_input = browser.page.query_selector("input[type='email'], input[name*='email' i]")
        if not email_input:
            return False
        email_input.fill(email)
        for selector in (
            "button:has-text('Continue')", "button:has-text('Next')",
            "[role='button']:has-text('Continue')",
        ):
            button = browser.page.query_selector(selector)
            if button and button.is_visible():
                button.click(timeout=10000)
                browser.wait_for_navigation()
                return True
    except Exception:
        return False
    return False


def _inspect_form_page(browser: BrowserSession, url: str) -> dict[str, Any]:
    """Inspect the rendered page and accept only a genuinely fillable flow."""
    try:
        body = browser.get_page_text()
    except Exception:
        body = ""
    if CLOSED_PATTERNS.search(body):
        return {"status": "CLOSED", "url": browser.page.url, "fields": [], "fillable": False}
    if browser.is_captcha_present():
        return {"status": "BLOCKED_CAPTCHA", "url": browser.page.url, "fields": [], "fillable": False}
    if EMAIL_VERIFICATION_PATTERNS.search(body):
        return {
            "status": "EMAIL_VERIFICATION_REQUIRED", "url": browser.page.url,
            "fields": [], "fillable": False,
            "metadata": {"email_verification_required": True},
        }
    if browser.is_google_oauth_required() and not browser.find_form():
        return {"status": "BLOCKED_LOGIN", "url": browser.page.url, "fields": [], "fillable": False}
    html = browser.get_page_html()
    fields = extract_form_fields(html, browser.page.url or url)
    login_text = f"{browser.page.url} {body}".lower()
    login_controls = []
    try:
        login_controls = [
            " ".join(filter(None, [node.inner_text(), node.get_attribute("aria-label")])).strip().lower()
            for node in browser.page.query_selector_all("a,button,[role='button']")
        ]
    except Exception:
        login_controls = []
    login_text_is_navigation_only = bool(login_controls) and any(
        "sign in" in item or "log in" in item or item == "login" for item in login_controls
    ) and not any("password" in (f.label or f.name).lower() for f in fields)
    actionable = _form_is_actionable(browser)
    public_email_gate = _has_public_email_gate(fields, actionable)
    # Luma intentionally exposes sign-in on public pages. A visible sign-in link
    # is never enough to classify the event as authentication-gated.
    if _is_luma_url(browser.page.url or url) and public_email_gate:
        return {
            "status": "PUBLIC_EMAIL_GATE", "url": browser.page.url or url,
            "fields": fields, "fillable": False,
            "metadata": {"public_email_gate": True, "login_required": False},
        }
    if "/login" in login_text or "/auth/" in login_text or (
        ("sign in" in login_text or "log in" in login_text) and not login_text_is_navigation_only
    ) or any(
        "password" in (f.label or f.name).lower() for f in fields
    ):
        return {
            "status": "BLOCKED_LOGIN",
            "url": browser.page.url,
            "fields": fields,
            "fillable": False,
            "metadata": {"login_required": True, "oauth_required": browser.is_google_oauth_required()},
        }
    fillable = bool(fields) and actionable
    metadata = {
        "required_fields": [f.label for f in fields if f.required],
        "field_types": {f.label: f.field_type for f in fields},
        "field_count": len(fields),
        "page_count": 1,
        "conditional_logic": any(f.field_type == "conditional" for f in fields),
        "login_required": False,
        "oauth_required": browser.is_google_oauth_required(),
        "actionable_control_found": actionable,
    }
    return {
        "status": "READY" if fillable else "BLOCKED_FORM_NOT_FOUND",
        "url": browser.page.url or url,
        "fields": fields,
        "fillable": fillable,
        "metadata": metadata,
    }


def _extract_application_open_date(text: str) -> str:
    """Keep the organizer's announced opening phrase without guessing a date."""
    match = re.search(
        r"(?:applications?|registration)\s+(?:will\s+)?open(?:s|ing)?\s*(?:on|at)?\s*([^\n.;]{3,80})",
        text,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _platform_for_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "luma.com" in host:
        return "Luma"
    if "google." in host or "accounts.google" in host:
        return "Google"
    if "eightfold.ai" in host:
        return "Eightfold"
    if "sprintd.org" in host:
        return "Sprintd"
    if "typeform.com" in host:
        return "Typeform"
    if "eventbrite." in host:
        return "Eventbrite"
    return host or "unknown"


def _deadline_has_passed(value: str) -> bool:
    if not value:
        return False
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date() < datetime.now(timezone.utc).date()
        except ValueError:
            continue
    return False


def _flow_result(status: str, checked: list[str], path: list[dict], reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": status,
        "discovery_status": status,
        "source_pages_checked": list(dict.fromkeys(checked)),
        "application_discovery_path": path,
        "reason": reason,
        **extra,
    }


def discover_application_flow(event: dict, applicant_id: str) -> dict[str, Any]:
    """Follow a rendered official event-to-registration flow up to five hops."""
    sources: list[str] = []
    for key in ("event_url", "application_url", "hackathonhub_url"):
        value = str(event.get(key, "") or "")
        if not value.startswith("http") or not urlparse(value).netloc:
            continue
        if key == "hackathonhub_url" and "/events/http" in value:
            continue
        if value not in sources:
            sources.append(value)
    checked: list[str] = []
    path: list[dict] = []
    if not sources:
        return _flow_result("APPLICATION_DISCOVERY_FAILED", checked, path, "No source URL")
    if _deadline_has_passed(str(event.get("application_deadline", "") or "")):
        return _flow_result("APPLICATION_CLOSED", checked, path, "Stored application deadline has passed")

    queue: list[tuple[str, int, str]] = [(url, 0, "official event source") for url in sources]
    visited: set[str] = set()
    saw_action = False
    last_text = ""
    try:
        # Discovery must begin in a clean public context. Luma applications do
        # not inherit a browser profile or require account authentication.
        with BrowserSession() as browser:
            public_email_steps: set[str] = set()
            while queue:
                current, depth, action = queue.pop(0)
                canonical = current.split("#", 1)[0]
                if canonical in visited or depth > 5 or not urlparse(current).netloc:
                    continue
                visited.add(canonical)
                if not browser.navigate(current):
                    continue
                page_url = browser.page.url or current
                checked.append(page_url)
                path.append({"url": page_url, "action": action, "depth": depth})
                last_text = browser.get_page_text()
                inspection = _inspect_form_page(browser, current)
                if inspection.get("status") == "CLOSED":
                    return _flow_result("APPLICATION_CLOSED", checked, path, "Rendered page says registration is closed", url=page_url, provider=_platform_for_url(page_url))
                if inspection.get("status") == "BLOCKED_CAPTCHA":
                    return _flow_result("BLOCKED_CAPTCHA", checked, path, "CAPTCHA detected during discovery", url=page_url, provider=_platform_for_url(page_url))
                if inspection.get("status") == "PUBLIC_EMAIL_GATE":
                    if page_url in public_email_steps:
                        return _flow_result(
                            "APPLICATION_DISCOVERY_FAILED", checked, path,
                            "Public email step did not expose application questions",
                            url=page_url, provider=_platform_for_url(page_url),
                        )
                    profile = profile_manager.get(applicant_id)
                    if profile and _advance_public_email_gate(browser, profile.email):
                        public_email_steps.add(page_url)
                        after = browser.page.url or page_url
                        checked.append(after)
                        path.append({"url": after, "action": "enter applicant email and continue", "depth": depth + 1})
                        inspection = _inspect_form_page(browser, after)
                        page_url = browser.page.url or after
                    else:
                        return _flow_result(
                            "APPLICATION_DISCOVERY_FAILED", checked, path,
                            "Public email registration step could not be advanced reliably",
                            url=page_url, provider=_platform_for_url(page_url),
                        )
                if inspection.get("status") == "EMAIL_VERIFICATION_REQUIRED":
                    return _flow_result(
                        "EMAIL_VERIFICATION_REQUIRED", checked, path,
                        "The public registration flow requires an email verification code before questions are available",
                        url=page_url, provider=_platform_for_url(page_url),
                        metadata=inspection.get("metadata", {}),
                    )
                if inspection.get("status") == "BLOCKED_LOGIN":
                    platform = _platform_for_url(page_url)
                    profile = profile_manager.get(applicant_id)
                    auth_account = ""
                    if profile:
                        auth_account = profile.google_oauth_email if platform == "Google" else profile.email
                    auth_key = "junction" if "hackjunction.app" in page_url else ""
                    if auth_key:
                        mark_auth_expired(applicant_id, auth_key, page_url)
                    return _flow_result(
                        "AUTH_REQUIRED", checked, path,
                        "Authentication is required before the application can be inspected",
                        url=page_url, provider=platform,
                        auth_status="AUTH_SETUP_REQUIRED", auth_platform=platform,
                        auth_login_url=page_url, auth_account=auth_account,
                    )
                if inspection.get("fillable"):
                    inspection_extra = {key: value for key, value in inspection.items() if key != "status"}
                    return _flow_result(
                        "APPLICATION_FORM_FOUND", checked, path,
                        "Rendered form fields and an actionable control found",
                        **inspection_extra, provider=detect_form_provider(page_url), confidence=0.95,
                    )

                body = f"{page_url} {last_text}"
                if WAITLIST_PATTERNS.search(body) and not DISCOVERY_ACTION_PATTERNS.search(last_text):
                    return _flow_result("WAITLIST_ONLY", checked, path, "Only a waitlist/sold-out state is available", url=page_url, provider=_platform_for_url(page_url))
                open_date = _extract_application_open_date(last_text)
                if NOT_OPEN_PATTERNS.search(last_text) and not re.search(r"\b(?:open|register|apply)\b", last_text, re.IGNORECASE):
                    return _flow_result("APPLICATION_NOT_OPEN_YET", checked, path, "The official page announces a future opening", application_open_date=open_date, url=page_url, provider=_platform_for_url(page_url))

                candidate_actions: list[tuple[int, str, dict]] = []
                for item in browser.discovery_actions():
                    label = item.get("text", "").strip()
                    href = item.get("href", "")
                    combined = f"{label} {href}"
                    if not DISCOVERY_ACTION_PATTERNS.search(combined):
                        continue
                    if any(word in label.lower() for word in ("submit application", "submit", "pay now")):
                        continue
                    saw_action = True
                    resolved = urljoin(page_url, href) if href else ""
                    resolved_canonical = resolved.split("#", 1)[0] if resolved else ""
                    if resolved_canonical == page_url.split("#", 1)[0]:
                        continue
                    lowered = label.lower()
                    priority = 0 if "click here to apply" in lowered else 1 if "apply" in lowered else 2 if "register" in lowered else 3
                    candidate_actions.append((priority, resolved or label, item))
                for _, _, item in sorted(candidate_actions, key=lambda entry: (entry[0], entry[1]))[:8]:
                    label = item.get("text", "").strip()
                    href = item.get("href", "")
                    resolved = urljoin(page_url, href) if href else ""
                    if resolved.startswith("http") and resolved.split("#", 1)[0] not in visited:
                        queue.append((resolved, depth + 1, label))
                    elif depth < 5 and browser.click_discovery_action(item):
                        after = browser.page.url or page_url
                        # Luma opens its public form in a same-URL dialog. Inspect
                        # that dialog immediately instead of discarding it as a
                        # duplicate navigation.
                        if _is_luma_url(after) and after.split("#", 1)[0] == page_url.split("#", 1)[0]:
                            path.append({"url": after, "action": f"open {label} form", "depth": depth + 1})
                            post_click = _inspect_form_page(browser, after)
                            if post_click.get("status") == "EMAIL_VERIFICATION_REQUIRED":
                                return _flow_result(
                                    "EMAIL_VERIFICATION_REQUIRED", checked, path,
                                    "The public registration flow requires an email verification code before questions are available",
                                    url=after, provider="Luma", metadata=post_click.get("metadata", {}),
                                )
                            if post_click.get("status") == "BLOCKED_CAPTCHA":
                                return _flow_result("BLOCKED_CAPTCHA", checked, path, "CAPTCHA detected during discovery", url=after, provider="Luma")
                            if post_click.get("status") == "BLOCKED_LOGIN":
                                return _flow_result("AUTH_REQUIRED", checked, path, "The application form is inaccessible without authentication", url=after, provider="Luma", auth_status="AUTH_SETUP_REQUIRED", auth_platform="Luma")
                            if post_click.get("fillable"):
                                post_extra = {key: value for key, value in post_click.items() if key != "status"}
                                post_extra["metadata"] = {
                                    **post_extra.get("metadata", {}),
                                    "public_luma_discovery": True,
                                    "authentication_required": False,
                                }
                                return _flow_result(
                                    "APPLICATION_FORM_FOUND", checked, path,
                                    "Public Luma application form fields found without authentication",
                                    **post_extra, provider=detect_form_provider(after), confidence=0.95,
                                )
                        elif after.split("#", 1)[0] not in visited:
                            queue.append((after, depth + 1, label))
    except Exception as exc:
        return _flow_result("APPLICATION_DISCOVERY_FAILED", checked, path, f"Browser discovery failed: {str(exc)[:200]}")

    if CLOSED_PATTERNS.search(last_text):
        return _flow_result("APPLICATION_CLOSED", checked, path, "Rendered official page indicates closure")
    if WAITLIST_PATTERNS.search(last_text):
        return _flow_result("WAITLIST_ONLY", checked, path, "Only a waitlist/sold-out state was found")
    open_date = _extract_application_open_date(last_text)
    if NOT_OPEN_PATTERNS.search(last_text):
        return _flow_result("APPLICATION_NOT_OPEN_YET", checked, path, "No application is open yet", application_open_date=open_date)
    if not saw_action:
        return _flow_result("APPLICATION_LINK_NOT_FOUND", checked, path, "No rendered application or registration action was found")
    return _flow_result("APPLICATION_DISCOVERY_FAILED", checked, path, "Application actions were found but no followable form or state was reached")


def _finalize_travel_status(event: dict) -> tuple[str, str, str]:
    """Separate Hub claims from official-source verification."""
    hub = str(event.get("hub_travel_status") or event.get("travel_support") or "UNKNOWN")
    source = str(event.get("travel_support_source") or "")
    official = str(event.get("official_travel_status") or "UNKNOWN")
    if source and "hackathonhub" not in source.lower():
        official = str(event.get("travel_support") or official)
    confirmed = {"CONFIRMED_FLIGHTS", "CONFIRMED_TRAVEL_REIMBURSEMENT", "CONFIRMED_TRAVEL_STIPEND"}
    if official in confirmed:
        final = official
    elif source and "hackathonhub" in source.lower() and official in {"", "UNKNOWN"}:
        final = "HUB_ONLY_UNVERIFIED"
    elif hub in confirmed:
        final = "UNVERIFIED_TRAVEL_SUPPORT"
    else:
        final = official if official != "UNKNOWN" else hub
    return hub, official, final


def _update_blocked_application(event_id: str, applicant_id: str, status: str, reason: str, **updates: Any) -> None:
    updates.update({"status": status, "notes": reason})
    update_application(event_id, applicant_id, updates)
    log_audit(event_id, status, reason, applicant_id=applicant_id)


def _should_refresh_discovery(event: dict, existing: dict | None) -> bool:
    """Invalidate stale Luma auth blockers after public-flow support was added."""
    if not existing:
        return True
    urls = " ".join(str(value or "") for value in (
        event.get("event_url"), event.get("application_url"), existing.get("application_url"),
    ))
    stale_auth = {
        "APPLICATION_LOGIN_REQUIRED", "AUTH_SETUP_REQUIRED", "AUTH_REQUIRED",
    }
    metadata = _json_value(existing.get("form_metadata"), {})
    return "luma.com" in urls.lower() and (
        str(existing.get("status", "")) in stale_auth
        or str(existing.get("discovery_status", "")) in stale_auth
        or not bool(metadata.get("public_luma_discovery"))
    )


def prepare_ready_to_apply(preflight: bool = False, event_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """Complete discovery, form, answer and safety gates before live queueing."""
    prepared: list[dict[str, Any]] = []
    events = sorted(get_all_events(), key=lambda e: _safe_float(e.get("event_score")), reverse=True)
    for event in events:
        if event_ids is not None and event.get("event_id") not in event_ids:
            continue
        event_score = _safe_float(event.get("event_score"))
        hub, official, final_travel = _finalize_travel_status(event)
        update_event(event["event_id"], {
            "hub_travel_status": hub,
            "official_travel_status": official,
            "final_travel_status": final_travel,
        })
        for applicant_id in profile_manager.applicant_ids:
            profile = profile_manager.get(applicant_id)
            if not profile:
                continue
            existing = get_application(event["event_id"], applicant_id)
            if existing and existing.get("status") in {"APPLIED", *UNCERTAIN_SUBMISSION_STATUSES}:
                continue
            reuse_cached_fit = False  # Apply-score recalculation must use current scoring rules.
            if reuse_cached_fit and event_ids is not None and existing and existing.get("discovery_status"):
                fit = {
                    "fit_score": existing.get("applicant_fit_score", 0),
                    "eligibility_status": existing.get("eligibility_status", "UNCERTAIN"),
                    "eligibility_reasoning": existing.get("eligibility_reasoning", ""),
                    "eligibility_confidence": existing.get("eligibility_confidence", 0.0),
                    "eligibility_source_evidence": existing.get("eligibility_source_evidence", ""),
                    "eligibility_requirements": _json_value(existing.get("eligibility_requirements"), []),
                    "eligible": existing.get("eligibility_status") == "ELIGIBLE",
                    "travel_eligible": bool(existing.get("travel_eligible")),
                }
            else:
                fit = score_applicant_fit(event["event_id"], applicant_id)
            eligibility = fit.get("eligibility_status", "ELIGIBLE" if fit.get("eligible") else "UNCERTAIN_MATERIAL_REQUIREMENT")
            base = {
                "application_id": existing.get("application_id") if existing else f"app_{event['event_id'][:12]}_{applicant_id}",
                "event_id": event["event_id"], "applicant_id": applicant_id,
                "applicant_name": profile.name, "event_score": event_score,
                "applicant_fit_score": fit.get("fit_score", 0),
                "eligibility_status": eligibility,
                "eligibility_reasoning": fit.get("eligibility_reasoning", fit.get("reasoning", "")),
                "eligibility_confidence": fit.get("eligibility_confidence", fit.get("confidence", 0.0)),
                "eligibility_source_evidence": fit.get("eligibility_source_evidence", ""),
                "eligibility_requirements": fit.get("eligibility_requirements", []),
                "travel_eligible": 1 if fit.get("travel_eligible") else 0,
                "travel_support_requested": 1,
                "travel_support_status": final_travel,
                "duplicate_check_passed": 1 if not (existing and existing.get("status") in {"APPLIED", *UNCERTAIN_SUBMISSION_STATUSES}) else 0,
            }
            if not existing:
                insert_application({**base, "status": "QUALIFIED"})
            else:
                update_application(event["event_id"], applicant_id, base)
            if eligibility not in LIVE_ELIGIBLE_STATES or not fit.get("eligible", False):
                status = "BLOCKED_ELIGIBILITY_UNCERTAIN" if eligibility.startswith("UNCERTAIN") else "INELIGIBLE"
                _update_blocked_application(event["event_id"], applicant_id, status, base["eligibility_reasoning"] or status)
                continue

            stored_status = str(existing.get("discovery_status", "") or "") if existing else ""
            stored_questions = _json_value(existing.get("questions"), []) if existing else []
            if event_ids is not None and stored_status and not _should_refresh_discovery(event, existing):
                stored_fields = []
                for item in stored_questions if isinstance(stored_questions, list) else []:
                    try:
                        stored_fields.append(FormField(**item))
                    except Exception:
                        continue
                flow = {
                    "status": stored_status,
                    "discovery_status": stored_status,
                    "url": existing.get("application_url", ""),
                    "provider": existing.get("form_provider", ""),
                    "source_pages_checked": _json_value(existing.get("source_pages_checked"), []),
                    "application_discovery_path": _json_value(existing.get("application_discovery_path"), []),
                    "reason": existing.get("application_discovery_reason", ""),
                    "metadata": _json_value(existing.get("form_metadata"), {}),
                    "fields": stored_fields,
                    "fillable": bool(existing.get("form_fillable")),
                    "application_open_date": existing.get("application_open_date", ""),
                    "auth_status": existing.get("auth_status", ""),
                    "auth_platform": existing.get("auth_platform", ""),
                    "auth_login_url": existing.get("auth_login_url", ""),
                    "auth_account": existing.get("auth_account", ""),
                }
            else:
                flow = discover_application_flow(event, applicant_id)
            common = {
                "application_url": flow.get("url", ""),
                "form_provider": flow.get("provider", ""),
                "application_discovery_source": flow.get("source", ""),
                "application_discovery_confidence": flow.get("confidence", 0.0),
                "application_discovery_reason": flow.get("reason", ""),
                "application_discovery_path": flow.get("application_discovery_path", []),
                "discovery_status": flow.get("discovery_status", flow.get("status", "")),
                "application_open_date": flow.get("application_open_date", ""),
                "last_application_check": datetime.now(timezone.utc).isoformat(),
                "next_application_check": flow.get("next_application_check", ""),
                "auth_status": flow.get("auth_status", ""),
                "auth_platform": flow.get("auth_platform", ""),
                "auth_login_url": flow.get("auth_login_url", ""),
                "auth_account": flow.get("auth_account", ""),
                "source_pages_checked": flow.get("source_pages_checked", []),
                "form_metadata": flow.get("metadata", {}),
                "application_open": 1 if flow.get("status") in {"READY", "APPLICATION_FORM_FOUND"} else 0,
                "form_fillable": 1 if flow.get("fillable") else 0,
                "travel_support_status": final_travel,
            }
            if flow.get("status") not in {"READY", "APPLICATION_FORM_FOUND"}:
                blocked_status = "AUTH_SETUP_REQUIRED" if flow.get("status") == "AUTH_REQUIRED" else flow.get("status", "APPLICATION_DISCOVERY_FAILED")
                _update_blocked_application(event["event_id"], applicant_id, blocked_status, flow.get("reason", "Application discovery failed"), **common)
                continue

            fields: list[FormField] = flow["fields"]
            themes = _json_value(event.get("themes"), [])
            context = {
                "event_name": event.get("event_name", ""), "themes": themes,
                "travel_support": final_travel, "organizer": event.get("organizer", ""),
                "description": (event.get("description", "") or "")[:1000],
                "city": event.get("city", ""), "country": event.get("country", ""),
            }
            answers: list[dict] = []
            for field in fields:
                field.answer = generate_answer_for_field(field, profile, context, use_llm=True)
                answers.append({"label": field.label, "answer": field.answer, "source": field.answer_source})
            questions = [field.model_dump() for field in fields]
            issues = validate_application(fields)
            fact_issues = validate_factual_consistency(fields, answers, profile)
            cross_issues = _cross_profile_issues(applicant_id, answers)
            consent_choices = []
            for field in fields:
                choice, category = consent_policy_for_field(field, profile)
                if category != "NOT_CONSENT":
                    consent_choices.append({"question": field.label, "choice": choice, "category": category})
            unreadable = [field for field in fields if field.required and not (field.label or field.name or field.selector)]
            common.update({
                "questions": questions, "answers": answers,
                "fact_check_passed": 1 if not fact_issues else 0,
                "cross_profile_check_passed": 1 if not cross_issues else 0,
                "duplicate_check_passed": 1,
                "consent_policy_passed": 1 if all(item["choice"] != "UNKNOWN_REQUIRED_FIELD" for item in consent_choices) else 0,
                "unreadable_required_fields": len(unreadable),
                "apply_score": fit.get("apply_score", 0),
                "travel_score": fit.get("apply_breakdown", {}).get("travel_value", 0),
                "form_metadata": {**common.get("form_metadata", {}), "consent_choices": consent_choices},
            })
            if issues:
                _update_blocked_application(event["event_id"], applicant_id, "BLOCKED_FORM_VALIDATION", "; ".join(issues), **common)
                continue
            if fact_issues:
                _update_blocked_application(event["event_id"], applicant_id, "BLOCKED_FACT_CHECK", "; ".join(fact_issues), **common)
                continue
            if cross_issues:
                _update_blocked_application(event["event_id"], applicant_id, "BLOCKED_CROSS_PROFILE", "; ".join(cross_issues), **common)
                continue
            if unreadable:
                _update_blocked_application(event["event_id"], applicant_id, "BLOCKED_UNREADABLE_FIELD", "Required form field lacks reliable question text", **common)
                continue
            if not common["consent_policy_passed"]:
                _update_blocked_application(event["event_id"], applicant_id, "BLOCKED_CONSENT_POLICY", "Required consent cannot be safely accepted", **common)
                continue
            if (existing or {}).get("form_validation_status") != "VALIDATED":
                _update_blocked_application(
                    event["event_id"], applicant_id, "FORM_VALIDATION_REQUIRED",
                    "A real submission-disabled browser form validation is required before READY_TO_APPLY",
                    **common,
                )
                continue
            # The individual phase only establishes hard safety gates.  The
            # score threshold is deliberately evaluated once for the pair.
            update_application(event["event_id"], applicant_id, {
                **common, "status": "TEAM_PENDING",
                "notes": "Individual hard gates passed; awaiting joint team decision",
            })
        prepared.extend(_finalize_team_decision(event, final_travel))
    prepared.sort(key=lambda item: (_safe_float(item["event_score"]) + 1.5 * _safe_float(item["fit_score"])), reverse=True)
    print(f"  READY_TO_APPLY prepared: {len(prepared)}")
    for item in prepared:
        print(
            f"  TEAM READY: {item['event_name']} | {item['applicant_name']} | "
            f"apply={item.get('apply_score', 0):.1f} | team_apply={item.get('team_apply_score', 0):.2f} | "
            f"url={item['application_url']} | provider={item['form_provider']} | "
            f"event={item['event_score']:.1f} | fit={item['fit_score']:.1f} | "
            f"travel={item['travel_support']} | decision={item.get('team_decision', TEAM_READY_STATUS)}"
        )
    return prepared


TARGETED_EVENT_HINTS = (
    "junction 2026",
    "bcg platinion",
    "data for good",
    "construction robotics",
    "robotics x ai",
    "european defense tech",
    "estonian defence week",
    "nordic hackathon",
    "future pioneers",
    "jugend hackt hamburg",
)


def select_targeted_event_ids() -> list[str]:
    """Select the requested deep-discovery set without scanning all events."""
    events = get_all_events()
    selected: list[dict] = []
    for hint in TARGETED_EVENT_HINTS:
        matches = [event for event in events if hint in str(event.get("event_name", "")).lower()]
        if hint == "european defense tech":
            matches = [event for event in matches if _safe_float(event.get("event_score")) > 0]
        if matches:
            selected.append(max(matches, key=lambda event: _safe_float(event.get("event_score"))))
    seen = {event.get("event_id") for event in selected}
    for event in sorted(events, key=lambda item: _safe_float(item.get("event_score")), reverse=True):
        if len(selected) >= 10:
            break
        if event.get("event_id") not in seen:
            selected.append(event)
            seen.add(event.get("event_id"))
    return [event["event_id"] for event in selected[:10]]


def _stored_discovery_flow(existing: dict) -> dict[str, Any]:
    stored_fields = []
    stored_questions = _json_value(existing.get("questions"), [])
    for item in stored_questions if isinstance(stored_questions, list) else []:
        try:
            stored_fields.append(FormField(**item))
        except Exception:
            continue
    stored_status = existing.get("discovery_status", "APPLICATION_DISCOVERY_FAILED")
    # Normalize legacy wording from before behavioral auth detection.
    if stored_status == "APPLICATION_LOGIN_REQUIRED":
        stored_status = "AUTH_REQUIRED"
    return {
        "status": stored_status,
        "discovery_status": stored_status,
        "url": existing.get("application_url", ""),
        "provider": existing.get("form_provider", ""),
        "source_pages_checked": _json_value(existing.get("source_pages_checked"), []),
        "application_discovery_path": _json_value(existing.get("application_discovery_path"), []),
        "reason": existing.get("application_discovery_reason", ""),
        "metadata": _json_value(existing.get("form_metadata"), {}),
        "fields": stored_fields,
        "fillable": bool(existing.get("form_fillable")),
        "application_open_date": existing.get("application_open_date", ""),
        "auth_status": existing.get("auth_status", ""),
        "auth_platform": existing.get("auth_platform", ""),
        "auth_login_url": existing.get("auth_login_url", ""),
        "auth_account": existing.get("auth_account", ""),
    }


def run_targeted_application_discovery() -> list[dict[str, Any]]:
    """Deeply inspect only the ten requested events and persist their flow state."""
    reports: list[dict[str, Any]] = []
    selected_ids = select_targeted_event_ids()
    for event_id in selected_ids:
        event = get_event_by_id(event_id)
        if not event:
            continue
        hub_travel, official_travel, final_travel = _finalize_travel_status(event)
        update_event(event_id, {
            "hub_travel_status": hub_travel,
            "official_travel_status": official_travel,
            "final_travel_status": final_travel,
        })
        applicant_flows: dict[str, dict[str, Any]] = {}
        for applicant_id in profile_manager.applicant_ids:
            profile = profile_manager.get(applicant_id)
            if not profile:
                continue
            existing = get_application(event_id, applicant_id)
            if existing and existing.get("discovery_status") and not _should_refresh_discovery(event, existing):
                fit = {
                    "fit_score": existing.get("applicant_fit_score", 0),
                    "eligibility_status": existing.get("eligibility_status", "UNCERTAIN"),
                    "eligibility_reasoning": existing.get("eligibility_reasoning", ""),
                }
            else:
                fit = score_applicant_fit(event_id, applicant_id)
            if existing and existing.get("discovery_status") and not _should_refresh_discovery(event, existing):
                flow = _stored_discovery_flow(existing)
            else:
                flow = discover_application_flow(event, applicant_id)
            applicant_flows[applicant_id] = flow
            if not existing:
                insert_application({
                    "application_id": f"app_{event_id[:12]}_{applicant_id}",
                    "event_id": event_id, "applicant_id": applicant_id,
                    "applicant_name": profile.name, "event_score": event.get("event_score", 0),
                    "applicant_fit_score": fit.get("fit_score", 0),
                    "eligibility_status": fit.get("eligibility_status", "UNCERTAIN"),
                    "eligibility_reasoning": fit.get("eligibility_reasoning", ""),
                    "status": flow.get("status", "APPLICATION_DISCOVERY_FAILED"),
                })
            update_application(event_id, applicant_id, {
                "application_url": flow.get("url", ""),
                "questions": [field.model_dump() for field in flow.get("fields", [])] if flow.get("fields") else [],
                "applicant_fit_score": fit.get("fit_score", 0),
                "eligibility_status": fit.get("eligibility_status", "UNCERTAIN"),
                "eligibility_reasoning": fit.get("eligibility_reasoning", ""),
                "form_provider": flow.get("provider", ""),
                "application_discovery_source": flow.get("source", event.get("event_url", "")),
                "application_discovery_confidence": flow.get("confidence", 0.0),
                "application_discovery_reason": flow.get("reason", ""),
                "application_discovery_path": flow.get("application_discovery_path", []),
                "discovery_status": flow.get("discovery_status", flow.get("status", "")),
                "source_pages_checked": flow.get("source_pages_checked", []),
                "form_metadata": flow.get("metadata", {}),
                "form_fillable": 1 if flow.get("fillable") else 0,
                "application_open": 1 if flow.get("status") == "APPLICATION_FORM_FOUND" else 0,
                "application_open_date": flow.get("application_open_date", ""),
                "last_application_check": datetime.now(timezone.utc).isoformat(),
                "next_application_check": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
                "auth_status": flow.get("auth_status", ""),
                "auth_platform": flow.get("auth_platform", ""),
                "auth_login_url": flow.get("auth_login_url", ""),
                "auth_account": flow.get("auth_account", ""),
                "status": "AUTH_SETUP_REQUIRED" if flow.get("status") == "AUTH_REQUIRED" else flow.get("status", "APPLICATION_DISCOVERY_FAILED"),
                "notes": flow.get("reason", ""),
            })
        states = sorted({flow.get("status", "") for flow in applicant_flows.values() if flow})
        representative = next(iter(applicant_flows.values()), {})
        update_event(event_id, {
            "application_open_date": representative.get("application_open_date", ""),
            "last_application_check": datetime.now(timezone.utc).isoformat(),
            "next_application_check": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
        })
        reports.append({
            "event_id": event_id, "event_name": event.get("event_name", ""),
            "official_url": event.get("event_url", ""),
            "event_score": _safe_float(event.get("event_score")),
            "application_deadline": event.get("application_deadline", ""),
            "travel_support": final_travel,
            "states": states, "applicant_flows": applicant_flows,
        })
    return reports


LUMA_VALIDATION_EVENT_NAMES = (
    "ROBOTICS X AI - HACKLAB",
    "Naval Defense Tech Hackathon - Galati",
    "European Defense Tech Hackathon - Amsterdam",
)


def _open_luma_public_form(browser: BrowserSession, event_url: str) -> tuple[list[FormField], list[dict[str, Any]], str]:
    """Open a public Luma form and stop before its final submission action."""
    path: list[dict[str, Any]] = []
    if not browser.navigate(event_url):
        return [], path, "Could not navigate to Luma event"
    clicked_actions: set[str] = set()
    for depth in range(3):
        html = browser.get_page_html()
        fields = extract_form_fields(html, browser.page.url or event_url)
        rendered = browser.get_rendered_form_fields()
        for index, field in enumerate(fields):
            if index >= len(rendered) or field.label:
                continue
            candidate = rendered[index].label.strip()
            if re.match(r"(?:what|how|are|do|can|is|i agree|i confirm)|.*\?", candidate, re.IGNORECASE):
                field.label = candidate
                field.description = candidate
                field.selector = rendered[index].selector
        if not fields:
            fields = rendered
        if fields:
            return fields, path, ""
        candidates = []
        for action in browser.discovery_actions():
            label = action.get("text", "").strip()
            if (
                label not in clicked_actions
                and DISCOVERY_ACTION_PATTERNS.search(label)
                and not re.search(r"\bsubmit\b", label, re.IGNORECASE)
            ):
                candidates.append(action)
        if not candidates:
            return [], path, "No public registration action found"
        def priority(action: dict[str, Any]) -> tuple[int, str]:
            label = action.get("text", "").lower()
            if "hacker" in label or "participant" in label:
                return (0, label)
            if "request to join" in label:
                return (1, label)
            if "apply" in label or "register" in label:
                return (2, label)
            if "community" in label or "member" in label:
                return (9, label)
            return (5, label)
        opened = False
        for action in sorted(candidates, key=priority):
            label = action.get("text", "").strip()
            clicked_actions.add(label)
            if browser.click_discovery_action(action):
                path.append({"url": browser.page.url or event_url, "action": label, "depth": depth + 1})
                opened = True
                break
        if not opened:
            return [], path, "Could not open a public application action"
    return [], path, "Public form did not become available within three safe steps"


def validate_luma_form_fill(event_names: tuple[str, ...] = LUMA_VALIDATION_EVENT_NAMES) -> list[dict[str, Any]]:
    """Fill safe public Luma fields in a real browser, never clicking final Submit.

    This is intentionally separate from the live queue. It is a DRY_RUN-only
    diagnostic and refuses to run if DRY_RUN is disabled.
    """
    if not settings.DRY_RUN:
        raise RuntimeError("Luma form-fill validation requires DRY_RUN=true")

    reports: list[dict[str, Any]] = []
    all_events = get_all_events()
    for event_name in event_names:
        event = next((item for item in all_events if item.get("event_name") == event_name), None)
        if not event:
            reports.append({"event": event_name, "status": "EVENT_NOT_FOUND"})
            continue
        if not _is_luma_url(str(event.get("event_url", ""))):
            reports.append({"event": event_name, "status": "NOT_LUMA"})
            continue
        for applicant_id in profile_manager.applicant_ids:
            profile = profile_manager.get(applicant_id)
            if profile and not get_application(event["event_id"], applicant_id):
                # Validation is evidence-producing on its own; it must not
                # silently lose a result merely because discovery did not yet
                # create an application row for this applicant/event pair.
                insert_application({
                    "event_id": event["event_id"],
                    "applicant_id": applicant_id,
                    "applicant_name": profile.name,
                    "application_url": event.get("event_url", ""),
                    "form_provider": "luma",
                    "status": "FORM_VALIDATION_REQUIRED",
                    "notes": "Created for submission-disabled Luma form validation",
                })
            fit = score_applicant_fit(event["event_id"], applicant_id)
            report: dict[str, Any] = {
                "event": event_name,
                "event_id": event["event_id"],
                "applicant": profile.name if profile else applicant_id,
                "applicant_id": applicant_id,
                "application_url": event.get("event_url", ""),
                "eligibility": fit.get("eligibility_status", "UNCERTAIN"),
                "questions": [], "answers": [], "fields_filled": 0,
                "fact_check_passed": False, "cross_profile_check_passed": False,
                "final_submit_not_clicked": True, "status": "SKIPPED",
                "blocker": "",
            }
            if not profile:
                report["blocker"] = "Applicant profile missing"
                reports.append(report)
                continue
            try:
                with BrowserSession() as browser:
                    fields, path, open_error = _open_luma_public_form(browser, event.get("event_url", ""))
                    report["discovery_path"] = path
                    if open_error:
                        report["blocker"] = open_error
                        update_application(event["event_id"], applicant_id, {
                            "form_validation_status": "BROWSER_BLOCKED",
                            "form_validated_at": datetime.now(timezone.utc).isoformat(),
                            "notes": open_error,
                        })
                        reports.append(report)
                        continue
                    report["questions"] = [field.model_dump() for field in fields]
                    if fit.get("eligibility_status") not in LIVE_ELIGIBLE_STATES:
                        report["status"] = "SKIPPED_ELIGIBILITY"
                        report["blocker"] = f"Eligibility is {fit.get('eligibility_status', 'UNCERTAIN')}; answers and form filling skipped"
                        reports.append(report)
                        continue
                    context = {
                        "event_name": event_name,
                        "themes": _json_value(event.get("themes"), []),
                        "organizer": event.get("organizer", ""),
                        "description": (event.get("description", "") or "")[:1000],
                        "city": event.get("city", ""), "country": event.get("country", ""),
                    }
                    consent_choices=[]
                    for field in fields:
                        field.answer = generate_answer_for_field(field, profile, context, use_llm=True)
                        choice, category = consent_policy_for_field(field, profile)
                        if category != "NOT_CONSENT": consent_choices.append({"question": field.label, "choice": choice, "category": category})
                    answers = [{"label": field.label, "answer": field.answer} for field in fields]
                    fact_issues = validate_factual_consistency(fields, answers, profile)
                    cross_issues = _cross_profile_issues(applicant_id, answers)
                    validation_issues = validate_application(fields)
                    report.update({
                        "answers": answers,
                        "fact_check_passed": not fact_issues,
                        "cross_profile_check_passed": not cross_issues,
                        "validation_issues": validation_issues,
                        "fact_issues": fact_issues,
                        "cross_profile_issues": cross_issues,
                        "consent_choices": consent_choices,
                        "consent_policy_passed": all(item["choice"] != "UNKNOWN_REQUIRED_FIELD" for item in consent_choices),
                    })
                    form_fill_issues: list[str] = []
                    for field in fields:
                        if field.answer in {"", "UNKNOWN_REQUIRED_FIELD"}:
                            continue
                        if browser.fill_field(field):
                            report["fields_filled"] += 1
                        elif field.required:
                            form_fill_issues.append(f"Required field could not be reliably filled: {field.label or field.selector}")
                    validation_issues.extend(form_fill_issues)
                    report["status"] = "VALIDATED" if not (fact_issues or cross_issues or validation_issues) else "VALIDATION_BLOCKED"
                    if report["status"] == "VALIDATION_BLOCKED":
                        report["blocker"] = "; ".join(validation_issues + fact_issues + cross_issues)
                    update_application(event["event_id"], applicant_id, {
                        "form_validation_status": report["status"],
                        "form_validated_at": datetime.now(timezone.utc).isoformat(),
                        "questions": report["questions"],
                        "answers": answers,
                        "fact_check_passed": int(report["fact_check_passed"]),
                        "cross_profile_check_passed": int(report["cross_profile_check_passed"]),
                        "consent_policy_passed": int(report["consent_policy_passed"]),
                        "unreadable_required_fields": sum(
                            1 for field in fields
                            if field.required and not (field.label or field.name or field.selector)
                        ),
                    })
            except Exception as exc:
                report["status"] = "BROWSER_BLOCKED"
                report["blocker"] = str(exc)[:200]
                update_application(event["event_id"], applicant_id, {
                    "form_validation_status": "BROWSER_BLOCKED",
                    "form_validated_at": datetime.now(timezone.utc).isoformat(),
                    "notes": report["blocker"],
                })
            reports.append(report)
    return reports


def validate_live_eligibility(event_id: str, applicant_id: str) -> dict[str, Any]:
    """Run all pre-submission safety gates. Returns {passes, checks, blockers, fit_score, event_score}."""
    checks = {}
    blockers = []
    event = get_event_by_id(event_id)
    profile = profile_manager.get(applicant_id)
    if not event:
        return {"passes": False, "checks": {}, "blockers": ["Event not found"]}
    if not profile:
        return {"passes": False, "checks": {}, "blockers": ["Profile not found"]}

    extra_raw = event.get("extra_data", "{}")
    try:
        extra = json.loads(extra_raw) if isinstance(extra_raw, str) else extra_raw
    except (json.JSONDecodeError, TypeError):
        extra = {}
    reg_status = extra.get("registration_status", "")
    checks["application_open"] = reg_status != "closed"
    if reg_status == "closed":
        blockers.append("Application closed")

    event_score = float(event.get("event_score", 0) or 0)
    fit = score_applicant_fit(event_id, applicant_id)
    checks["eligible"] = fit.get("eligible", False) and fit.get("eligibility_status") in LIVE_ELIGIBLE_STATES
    if not checks["eligible"]:
        blockers.append(f"Not eligible: {fit.get('eligibility_reasoning', 'Unknown')}")

    checks["schedule_conflict"] = event.get("status") != "SCHEDULE_CONFLICT"
    if not checks["schedule_conflict"]:
        blockers.append("Schedule conflict")

    team_apply_score = _safe_float(event.get("team_apply_score"))
    team = load_team()
    checks["team_apply_score"] = team_apply_score >= team.minimum_team_score
    if not checks["team_apply_score"]:
        blockers.append(f"Team apply score {team_apply_score:.2f} < {team.minimum_team_score}")

    if settings.LIVE_REQUIRE_CONFIRMED_TRAVEL:
        ts = event.get("travel_support", "")
        checks["confirmed_travel"] = ts in (
            "CONFIRMED_FLIGHTS", "CONFIRMED_TRAVEL_REIMBURSEMENT", "CONFIRMED_TRAVEL_STIPEND"
        )
        if not checks["confirmed_travel"]:
            blockers.append(f"Travel not confirmed: {ts}")

    existing = get_application(event_id, applicant_id)
    team_members = {app.get("applicant_id"): app for app in get_applications_for_event(event_id)}
    ready_checks = {
        "ready_to_apply": bool(existing and existing.get("status") == "READY_TO_APPLY"),
        "application_url": bool(existing and str(existing.get("application_url", "")).startswith("http")),
        "form_provider": bool(existing and existing.get("form_provider")),
        "form_fillable": bool(existing and existing.get("form_fillable")),
        "application_open": bool(existing and existing.get("application_open")),
        "fact_check_passed": bool(existing and existing.get("fact_check_passed")),
        "cross_profile_check_passed": bool(existing and existing.get("cross_profile_check_passed")),
        "duplicate_check_passed": bool(existing and existing.get("duplicate_check_passed")),
        "team_ready": event.get("team_status") == TEAM_READY_STATUS,
        "team_pair_complete": all(
            team_members.get(aid, {}).get("status") == "READY_TO_APPLY"
            and team_members.get(aid, {}).get("application_group_id") == event.get("team_application_group_id")
            for aid in team.members
        ),
        "form_validation_status": bool(existing and existing.get("form_validation_status") == "VALIDATED"),
    }
    checks.update(ready_checks)
    for name, passed in ready_checks.items():
        if not passed:
            blockers.append(f"READY_TO_APPLY gate failed: {name}")
    checks["no_duplicate"] = not (
        existing and existing.get("status") in ("APPLIED", *UNCERTAIN_SUBMISSION_STATUSES)
    )
    if not checks["no_duplicate"]:
        blockers.append("Already applied")

    checks["travel_eligible"] = fit.get("travel_eligible", True)

    return {
        "passes": len(blockers) == 0,
        "checks": checks,
        "blockers": blockers,
        "fit_score": fit.get("fit_score", 0),
        "event_score": event_score,
    }


def create_submission_snapshot(event_id, applicant_id, fields, answers, eligibility):
    """Write pre-submission snapshot to disk. Returns file path."""
    snapshots_dir = Path("submission_snapshots")
    snapshots_dir.mkdir(exist_ok=True)
    event = get_event_by_id(event_id)
    profile = profile_manager.get(applicant_id)
    now = datetime.now(timezone.utc)
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', (event.get("event_name", "unknown") or "unknown")[:40])
    filename = f"{now.strftime('%Y-%m-%d')}_{safe_name}_{applicant_id}.json"
    path = snapshots_dir / filename
    if path.exists():
        # A retry must preserve the earlier pre-submit evidence rather than
        # overwrite it; this is especially important for manual overrides.
        path = snapshots_dir / f"{now.strftime('%Y-%m-%d_%H%M%S_%f')}_{safe_name}_{applicant_id}.json"

    questions_list = []
    for f in fields:
        if hasattr(f, 'model_dump'):
            questions_list.append(f.model_dump())
        elif isinstance(f, dict):
            questions_list.append(f)
        else:
            questions_list.append({"label": str(f)})

    snapshot = {
        "event_name": event.get("event_name", ""),
        "event_id": event_id,
        "applicant": applicant_id,
        "applicant_name": profile.name if profile else "",
        "team_application_group_id": (get_application(event_id, applicant_id) or {}).get("application_group_id", ""),
        "event_score": event.get("event_score", 0),
        "applicant_fit_score": eligibility.get("fit_score", 0),
        "travel_support_status": event.get("travel_support", ""),
        "application_url": event.get("application_url", event.get("event_url", "")),
        "email_used": profile.email if profile else "",
        "questions": questions_list,
        "answers": answers,
        "eligibility": {k: v for k, v in eligibility.items() if k != "checks"},
        "timestamp": now.isoformat(),
        "DRY_RUN": settings.DRY_RUN,
    }
    path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    return str(path)


def _form_structure_signature(fields: list[FormField]) -> list[tuple[str, str, bool]]:
    """Stable, privacy-free representation used to reject changed live forms."""
    return [
        (re.sub(r"\s+", " ", (field.label or field.name or "").strip().lower()), field.field_type, bool(field.required))
        for field in fields
    ]


def _luma_active_submit_button(page: Any) -> tuple[Any | None, dict[str, Any]]:
    """Find the one final submit control inside Luma's active registration form."""
    forms = page.locator("form").filter(has=page.locator("input[type='email']"))
    form_count = forms.count()
    buttons = forms.locator("button[type='submit']:visible, input[type='submit']:visible") if form_count else page.locator(":not(*)")
    button_count = buttons.count()
    details: dict[str, Any] = {
        "form_count": form_count,
        "button_count": button_count,
        "selector": "form:has(input[type=email]) >> button[type=submit]:visible, input[type=submit]:visible",
    }
    if form_count != 1 or button_count != 1:
        return None, details
    return buttons.first, details


def _luma_button_diagnostics(page: Any, button: Any, details: dict[str, Any]) -> dict[str, Any]:
    """Collect compact, non-sensitive actionability evidence for a failed click."""
    try:
        details.update({
            "text": button.inner_text()[:160],
            "tag": button.evaluate("e => e.tagName"),
            "visible": button.is_visible(),
            "enabled": button.is_enabled(),
            "box": button.bounding_box(),
            "html": button.evaluate("e => e.outerHTML.slice(0, 1200)"),
            "in_iframe": len(page.frames) > 1 and button.evaluate("e => e.ownerDocument !== document"),
            "top_element": button.evaluate("""e => {
                const r = e.getBoundingClientRect();
                const x = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                return x ? {tag: x.tagName, text: (x.innerText || x.getAttribute('aria-label') || '').slice(0, 120), same: x === e || e.contains(x)} : null;
            }"""),
        })
    except Exception as exc:
        details["diagnostic_error"] = str(exc)[:180]
    try:
        details["active_form_text"] = page.locator("form").filter(has=page.locator("input[type='email']")).inner_text()[:800]
    except Exception:
        details["active_form_text"] = ""
    details["page_url"] = page.url
    return details


def _capture_luma_click_failure(page: Any, event_id: str, applicant_id: str, details: dict[str, Any], error: str) -> str:
    """Persist diagnostics when no irreversible click has been confirmed."""
    snapshots_dir = Path("submission_snapshots")
    snapshots_dir.mkdir(exist_ok=True)
    base = snapshots_dir / f"click_failure_{event_id[:12]}_{applicant_id}"
    screenshot_error = ""
    try:
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception as exc:
        screenshot_error = str(exc)[:180]
    payload = {"error": error, "details": details, "screenshot_error": screenshot_error}
    diagnostic_path = base.with_suffix(".json")
    diagnostic_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return str(diagnostic_path)


def verify_live_luma_form_structure(event_id: str, applicant_id: str, expected_fields: list[FormField]) -> tuple[bool, str]:
    """Open the public Luma flow and reject a material form change before a snapshot/submit."""
    event = get_event_by_id(event_id) or {}
    app_url = str(event.get("event_url") or event.get("application_url") or "")
    try:
        with BrowserSession() as browser:
            live_fields, _, error = _open_luma_public_form(browser, app_url)
    except Exception as exc:
        return False, f"Could not reopen Luma form: {str(exc)[:160]}"
    if error:
        return False, error
    if _form_structure_signature(live_fields) != _form_structure_signature(expected_fields):
        return False, "Live Luma form structure materially changed from the validated preflight"
    return True, ""


def _execute_luma_live_submission(
    event_id: str,
    applicant_id: str,
    fields: list[FormField],
    answers: list[dict[str, Any]],
    eligibility: dict[str, Any],
    snapshot_path: str,
) -> dict[str, Any]:
    """Submit a public Luma form only after its current structure matches preflight."""
    profile = profile_manager.get(applicant_id)
    event = get_event_by_id(event_id) or {}
    result: dict[str, Any] = {
        "status": "BLOCKED", "confirmation_text": "", "confirmation_url": "",
        "confirmation_reference": "", "submit_clicked": False, "error": "",
        "click_phase": "NOT_STARTED",
    }

    def finish() -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        update_application(event_id, applicant_id, {
            "status": result["status"],
            "date_submitted": now if result["status"] == "APPLIED" else "",
            "application_confirmation": result.get("confirmation_text", ""),
            "application_reference": result.get("confirmation_reference", ""),
            "notes": result.get("error", ""),
        })
        log_audit(event_id, "LIVE_SUBMIT_COMPLETE", f"{applicant_id}: {result['status']} ({result['click_phase']})", applicant_id=applicant_id)
        return result

    if not profile:
        result["error"] = "Profile missing"
        return finish()
    try:
        with BrowserSession() as browser:
            live_fields, _, error = _open_luma_public_form(browser, str(event.get("event_url") or ""))
            if error or _form_structure_signature(live_fields) != _form_structure_signature(fields):
                result["error"] = error or "Live Luma form structure materially changed before Submit"
                return finish()
            answers_by_label = {str(item.get("label", "")).strip().lower(): str(item.get("answer", "")) for item in answers}
            for live_field in live_fields:
                live_field.answer = answers_by_label.get(live_field.label.strip().lower(), "")
                if live_field.field_type == "email":
                    live_field.answer = profile.email
                if live_field.required and not live_field.answer:
                    result["error"] = f"Required live field has no verified answer: {live_field.label or live_field.name}"
                    return finish()
                if live_field.answer and not browser.fill_field(live_field):
                    result["error"] = f"Could not reliably fill live field: {live_field.label or live_field.name}"
                    return finish()
            snapshots_dir = Path("submission_snapshots")
            snapshots_dir.mkdir(exist_ok=True)
            browser.page.screenshot(path=str(snapshots_dir / f"pre_submit_{event_id[:12]}_{applicant_id}.png"))
            update_application(event_id, applicant_id, {"status": "APPLYING", "submission_snapshot": {"snapshot_path": snapshot_path, "team_application_group_id": (get_application(event_id, applicant_id) or {}).get("application_group_id", "")}})
            button, button_details = _luma_active_submit_button(browser.page)
            log_audit(event_id, "BUTTON_FOUND", json.dumps(button_details), applicant_id=applicant_id)
            if not button:
                result["error"] = "Could not uniquely locate the active Luma form submit button"
                diagnostic_path = _capture_luma_click_failure(browser.page, event_id, applicant_id, button_details, result["error"])
                log_audit(event_id, "SUBMIT_CLICK_FAILED", f"{result['error']}; diagnostics={diagnostic_path}", "WARNING", applicant_id)
                return finish()
            try:
                # Targeted stability checks: the form must be visible, its final button
                # enabled, in view, and unobstructed before normal Playwright clicking.
                button.wait_for(state="visible", timeout=LIVE_CLICK_TIMEOUT_MS)
                button.scroll_into_view_if_needed(timeout=LIVE_CLICK_TIMEOUT_MS)
                log_audit(event_id, "BUTTON_SCROLLED_INTO_VIEW", applicant_id=applicant_id)
                button_details = _luma_button_diagnostics(browser.page, button, button_details)
                if not button_details.get("visible") or not button_details.get("enabled") or not button_details.get("top_element", {}).get("same"):
                    raise RuntimeError(f"Button is not actionable: {json.dumps(button_details)}")
                log_audit(event_id, "BUTTON_ACTIONABLE", json.dumps(button_details), applicant_id=applicant_id)
            except Exception as exc:
                result["error"] = f"BUTTON_NOT_ACTIONABLE: {str(exc)[:180]}"
                diagnostic_path = _capture_luma_click_failure(browser.page, event_id, applicant_id, button_details, result["error"])
                log_audit(event_id, "SUBMIT_CLICK_FAILED", f"{result['error']}; diagnostics={diagnostic_path}", "WARNING", applicant_id)
                return finish()

            observed_requests: list[dict[str, str]] = []
            observed_responses: list[dict[str, Any]] = []
            failed_requests: list[dict[str, str]] = []
            def observe_request(request: Any) -> None:
                if request.method.upper() in {"POST", "PUT", "PATCH"}:
                    observed_requests.append({"method": request.method, "url": request.url})
            def observe_response(response: Any) -> None:
                request = response.request
                if request.method.upper() in {"POST", "PUT", "PATCH"}:
                    # Store only method, URL, status, and content type.  Response
                    # bodies can contain registration tokens or applicant data.
                    observed_responses.append({
                        "method": request.method,
                        "url": response.url,
                        "status": response.status,
                        "content_type": response.headers.get("content-type", ""),
                    })
            def observe_request_failed(request: Any) -> None:
                if request.method.upper() in {"POST", "PUT", "PATCH"}:
                    failed_requests.append({"method": request.method, "url": request.url, "failure": request.failure or ""})
            browser.page.on("request", observe_request)
            browser.page.on("response", observe_response)
            browser.page.on("requestfailed", observe_request_failed)
            # These records deliberately surround only the irreversible Playwright
            # action, making later recovery possible without guessing.
            log_audit(event_id, "SUBMIT_INTENT_RECORDED", f"{applicant_id}: active Luma form submit", applicant_id=applicant_id)
            result["click_phase"] = "INTENT_RECORDED"
            log_audit(event_id, "CLICK_START", applicant_id=applicant_id)
            log_audit(event_id, "SUBMIT_CLICK_ISSUED", f"{applicant_id}: active Luma form submit", applicant_id=applicant_id)
            result["click_phase"] = "CLICK_ISSUED"
            click_completed = False
            try:
                # Register this waiter before clicking so a fast API response cannot
                # be missed. It is a targeted response wait, not a blanket sleep.
                with browser.page.expect_response(
                    lambda response: response.request.method == "POST" and "/event/register" in response.url,
                    timeout=LIVE_CONFIRMATION_TIMEOUT_MS,
                ) as registration_response:
                    button.click(timeout=LIVE_CLICK_TIMEOUT_MS)
                    click_completed = True
                    log_audit(event_id, "SUBMIT_CLICK_COMPLETED", "active Luma form submit", applicant_id=applicant_id)
                    log_audit(event_id, "CLICK_COMPLETED", applicant_id=applicant_id)
                    result["click_phase"] = "CLICK_COMPLETED"
                    result["submit_clicked"] = True
                    try:
                        browser.page.screenshot(path=str(snapshots_dir / f"post_click_{event_id[:12]}_{applicant_id}.png"))
                    except Exception as screenshot_exc:
                        log_audit(event_id, "POST_CLICK_SCREENSHOT_FAILED", str(screenshot_exc)[:180], "WARNING", applicant_id)
                    else:
                        log_audit(event_id, "POST_CLICK_SCREENSHOT_SAVED", applicant_id=applicant_id)
                registration = registration_response.value
                result["registration_response"] = {
                    "status": registration.status,
                    "url": registration.url,
                    "content_type": registration.headers.get("content-type", ""),
                }
                log_audit(event_id, "REGISTRATION_RESPONSE_RECEIVED", json.dumps(result["registration_response"]), applicant_id=applicant_id)
            except Exception as exc:
                if click_completed:
                    log_audit(event_id, "REGISTRATION_RESPONSE_TIMEOUT", str(exc)[:180], "WARNING", applicant_id)
                else:
                    result["error"] = f"CLICK_TIMEOUT_OR_FAILURE: {str(exc)[:180]}"
                    diagnostic_path = _capture_luma_click_failure(browser.page, event_id, applicant_id, _luma_button_diagnostics(browser.page, button, button_details), result["error"])
                    log_audit(event_id, "SUBMIT_CLICK_FAILED", f"{result['error']}; diagnostics={diagnostic_path}", "WARNING", applicant_id)
                    return finish()
            # A completed click and a confirmed submission are separate phases.  Luma
            # can keep the same URL and render its acknowledgement asynchronously.
            try:
                browser.page.wait_for_load_state("networkidle", timeout=LIVE_CONFIRMATION_TIMEOUT_MS)
            except Exception as exc:
                log_audit(event_id, "CONFIRMATION_WAIT_TIMEOUT", str(exc)[:180], "WARNING", applicant_id)
            result["network_submission_request_seen"] = bool(observed_requests)
            result["network_requests"] = observed_requests[:10]
            result["network_responses"] = observed_responses[:10]
            result["failed_requests"] = failed_requests[:10]
            log_audit(event_id, "NETWORK_SUBMISSION_REQUEST_SEEN", json.dumps(observed_requests[:10]), applicant_id=applicant_id)
            log_audit(event_id, "NETWORK_SUBMISSION_RESPONSE_SEEN", json.dumps(observed_responses[:10]), applicant_id=applicant_id)
            log_audit(event_id, "NETWORK_SUBMISSION_REQUEST_FAILED", json.dumps(failed_requests[:10]), applicant_id=applicant_id)
            confirmation = detect_confirmation(browser.page)
            try:
                browser.page.screenshot(path=str(snapshots_dir / f"confirmation_{event_id[:12]}_{applicant_id}.png"))
            except Exception as exc:
                log_audit(event_id, "CONFIRMATION_SCREENSHOT_FAILED", str(exc)[:180], "WARNING", applicant_id)
            if confirmation["confirmed"]:
                result.update({"status": "APPLIED", "confirmation_text": confirmation["text"], "confirmation_url": confirmation["url"], "confirmation_reference": confirmation["reference"]})
            else:
                result.update({"status": "SUBMITTED_CONFIRMATION_UNKNOWN", "error": "CLICK_COMPLETED but confirmation was not detected before CONFIRMATION_TIMEOUT"})
    except Exception as exc:
        result.update({"status": "BLOCKED", "error": str(exc)[:200]})
    return finish()


def detect_confirmation(page):
    """Detect submission confirmation from page state."""
    result = {"confirmed": False, "text": "", "url": page.url, "reference": ""}
    try:
        page_text = page.inner_text("body").lower()
    except Exception:
        return result
    for phrase in CONFIRMATION_PHRASES:
        if phrase in page_text:
            result["confirmed"] = True
            idx = page_text.find(phrase)
            start = max(0, idx - 50)
            end = min(len(page_text), idx + len(phrase) + 200)
            result["text"] = page_text[start:end].strip()
            break
    if not result["confirmed"]:
        url_lower = page.url.lower()
        for indicator in ["success", "confirm", "thank", "complete", "done", "submitted"]:
            if indicator in url_lower:
                result["confirmed"] = True
                try:
                    result["text"] = page.inner_text("body")[:500]
                except Exception:
                    pass
                break
    try:
        ref_match = re.search(
            r'(?:application|reference|confirmation|registration)\s*(?:#|id|number|code)[:\s]*([A-Za-z0-9_-]{4,})',
            page.inner_text("body"), re.IGNORECASE
        )
        if ref_match:
            result["reference"] = ref_match.group(1)
    except Exception:
        pass
    return result


def execute_live_submission(
    event_id: str,
    applicant_id: str,
    fields: list[FormField],
    answers: list[dict[str, Any]],
    eligibility: dict[str, Any],
    snapshot_path: str,
) -> dict[str, Any]:
    """Execute actual browser submission for a live application."""
    profile = profile_manager.get(applicant_id)
    event = get_event_by_id(event_id)
    if not profile or not event:
        return {"status": "BLOCKED", "error": "Profile or event missing", "submit_clicked": False,
                "confirmation_text": "", "confirmation_url": "", "confirmation_reference": ""}

    app_url = event.get("application_url") or event.get("event_url", "")
    if not app_url or not app_url.startswith("http"):
        return {"status": "BLOCKED", "error": "No valid application URL", "submit_clicked": False,
                "confirmation_text": "", "confirmation_url": "", "confirmation_reference": ""}
    if _is_luma_url(app_url):
        return _execute_luma_live_submission(event_id, applicant_id, fields, answers, eligibility, snapshot_path)

    result = {
        "status": "BLOCKED", "confirmation_text": "", "confirmation_url": "",
        "confirmation_reference": "", "submit_clicked": False, "error": "",
    }

    log_audit(event_id, "LIVE_SUBMIT_START", f"{applicant_id}: Starting", applicant_id=applicant_id)

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=settings.HEADLESS)
            page = browser.new_page()
            page.goto(app_url, wait_until="domcontentloaded", timeout=20000)
            time.sleep(2)

            captcha_sels = ["iframe[src*='recaptcha']", "iframe[src*='hcaptcha']", ".g-recaptcha", ".h-captcha"]
            for sel in captcha_sels:
                if page.query_selector(sel):
                    browser.close()
                    result["status"] = "BLOCKED_CAPTCHA"
                    result["error"] = "CAPTCHA detected"
                    log_audit(event_id, "BLOCKED_CAPTCHA", "CAPTCHA", applicant_id=applicant_id)
                    return result

            filled = 0
            for field in fields:
                try:
                    answer = field.answer if hasattr(field, 'answer') else ""
                    if not answer:
                        continue
                    name = field.name if hasattr(field, 'name') else ""
                    label = field.label if hasattr(field, 'label') else ""
                    ftype = field.field_type if hasattr(field, 'field_type') else "text"
                    if name:
                        selector = f"[name='{name}'], #{name}"
                    elif label:
                        selector = f"*[aria-label='{label}']"
                    else:
                        selector = "input, textarea"
                    if ftype in ("text", "textarea", "email", "phone", "url", "date"):
                        page.fill(selector, answer)
                        filled += 1
                    elif ftype == "checkbox":
                        cb = page.query_selector(selector)
                        if cb and answer.lower() in ("yes", "true", "1"):
                            cb.check()
                            filled += 1
                    time.sleep(0.3)
                except Exception as e:
                    print(f"  [live] Fill error: {e}")

            ss_dir = Path("submission_snapshots")
            ss_dir.mkdir(exist_ok=True)
            page.screenshot(path=str(ss_dir / f"pre_submit_{event_id[:12]}_{applicant_id}.png"))

            update_application(event_id, applicant_id, {
                "submission_snapshot": json.dumps({
                    "snapshot_path": snapshot_path,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "answers": answers,
                }),
                "status": "APPLYING",
            })

            log_audit(event_id, "SUBMIT_CLICK", f"{applicant_id}: Clicking submit", applicant_id=applicant_id)

            submit_clicked = False
            for btn_text in ["Submit", "Apply", "Send", "Register", "Absenden", "Invia"]:
                try:
                    btn = page.query_selector(f'button:has-text("{btn_text}"), input[type="submit"][value="{btn_text}"]')
                    if btn:
                        btn.click()
                        submit_clicked = True
                        result["submit_clicked"] = True
                        break
                except Exception:
                    pass
            if not submit_clicked:
                try:
                    page.keyboard.press("Enter")
                    submit_clicked = True
                    result["submit_clicked"] = True
                except Exception:
                    pass
            if not submit_clicked:
                browser.close()
                result["status"] = "BLOCKED"
                result["error"] = "Could not find submit button"
                return result

            time.sleep(3)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            confirmation = detect_confirmation(page)
            page.screenshot(path=str(ss_dir / f"post_submit_{event_id[:12]}_{applicant_id}.png"))
            browser.close()

            if confirmation["confirmed"]:
                result["status"] = "APPLIED"
                result["confirmation_text"] = confirmation["text"]
                result["confirmation_url"] = confirmation["url"]
                result["confirmation_reference"] = confirmation["reference"]
            else:
                result["status"] = "SUBMISSION_STATUS_UNKNOWN"
                result["error"] = "Submit clicked but no confirmation detected"
    except Exception as e:
        result["status"] = "BLOCKED"
        result["error"] = str(e)[:200]
        log_audit(event_id, "LIVE_SUBMIT_ERROR", str(e)[:200], applicant_id=applicant_id)

    now = datetime.now(timezone.utc).isoformat()
    update_application(event_id, applicant_id, {
        "status": result["status"],
        "date_submitted": now if result["status"] == "APPLIED" else "",
        "application_confirmation": result.get("confirmation_text", ""),
        "application_reference": result.get("confirmation_reference", ""),
        "notes": result.get("error", ""),
    })
    log_audit(event_id, "LIVE_SUBMIT_COMPLETE",
              f"{applicant_id}: {result['status']}", applicant_id=applicant_id)
    return result


def submission_disabled_executor(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Executor used by real-event preflight; it can never click Submit."""
    return {
        "status": "PREFLIGHT_ONLY",
        "confirmation_text": "[PREFLIGHT ONLY - Submit not clicked]",
        "confirmation_url": "",
        "confirmation_reference": "",
        "submit_clicked": False,
        "error": "Submission disabled for preflight",
    }


def build_live_submission_queue() -> list[dict[str, Any]]:
    """Build a queue only from complete, jointly approved team pairs."""
    queue = []
    all_apps = get_all_applications()
    by_event: dict[str, dict[str, dict]] = {}
    for app in all_apps:
        by_event.setdefault(app.get("event_id", ""), {})[app.get("applicant_id", "")] = app
    team_ready_keys: set[tuple[str, str]] = set()
    team = load_team()
    member_ids = set(team.members)
    for event_id, members in by_event.items():
        event = get_event_by_id(event_id)
        if not event or event.get("team_status") != TEAM_READY_STATUS:
            continue
        if not member_ids.issubset(members):
            continue
        group_id = event.get("team_application_group_id", "")
        if not group_id or any(members[member_id].get("application_group_id") != group_id for member_id in member_ids):
            continue
        if all(members[member_id].get("status") == "READY_TO_APPLY" for member_id in member_ids) and _safe_float(event.get("team_apply_score")) >= team.minimum_team_score:
            team_ready_keys.update({(event_id, member_id) for member_id in member_ids})
    for app in all_apps:
        if app.get("status") != "READY_TO_APPLY":
            continue
        if (app.get("event_id"), app.get("applicant_id")) not in team_ready_keys:
            _update_blocked_application(app["event_id"], app["applicant_id"], "TEAM_BLOCKED_FORM", "Application is not part of a complete TEAM_READY_TO_APPLY pair")
            continue
        missing = [key for key in READY_REQUIRED_FIELDS if not app.get(key)]
        for key in ("questions", "answers"):
            if key not in missing and not _has_json_content(app.get(key)):
                missing.append(key)
        if app.get("eligibility_status") not in LIVE_ELIGIBLE_STATES:
            missing.append("eligibility_status=acceptable")
        if not bool(app.get("consent_policy_passed")):
            missing.append("consent_policy_passed")
        if app.get("form_validation_status") != "VALIDATED":
            missing.append("form_validation_status=VALIDATED")
        if int(app.get("unreadable_required_fields") or 0) > 0:
            missing.append("unreadable_required_fields")
        if missing:
            reason = f"Incomplete READY_TO_APPLY record: {', '.join(missing)}"
            _update_blocked_application(app["event_id"], app["applicant_id"], "BLOCKED_NOT_READY", reason)
            continue
        event = get_event_by_id(app["event_id"])
        profile = profile_manager.get(app["applicant_id"])
        if not event or not profile:
            continue
        event_score = _safe_float(app.get("event_score"))
        fit_score = _safe_float(app.get("applicant_fit_score"))
        travel = event.get("final_travel_status") or app.get("travel_support_status") or "UNKNOWN"
        reasons = []
        if travel in {"CONFIRMED_FLIGHTS", "CONFIRMED_TRAVEL_REIMBURSEMENT", "CONFIRMED_TRAVEL_STIPEND"}:
            priority = 100
            reasons.append("official_verified_travel")
        elif travel == "UNVERIFIED_TRAVEL_SUPPORT":
            priority = 10
            reasons.append("unverified_travel_support")
        else:
            priority = 0
        priority += int(event_score * 1.2) + int(fit_score * 1.5)
        priority += 20  # application availability / reliable form was proven
        priority += int(_safe_float(app.get("eligibility_confidence")) * 20)
        deadline = event.get("application_deadline", "")
        if deadline:
            try:
                days = (datetime.strptime(str(deadline)[:10], "%Y-%m-%d").date() - datetime.now(timezone.utc).date()).days
                if days <= 2:
                    priority += 80
                    reasons.append("deadline_urgent")
                elif days <= 7:
                    priority += 40
                    reasons.append("deadline_soon")
            except (ValueError, TypeError):
                pass
        reasons.extend(["event_quality", "applicant_fit", "form_reliable"])
        queue.append({
            "event_id": app["event_id"], "event_name": event.get("event_name", ""),
            "applicant_id": app["applicant_id"], "applicant_name": profile.name,
            "priority": priority, "reason": "; ".join(reasons),
            "event_score": event_score, "fit_score": fit_score,
            "apply_score": _safe_float(app.get("apply_score")),
            "team_apply_score": _safe_float(event.get("team_apply_score")),
            "team_application_group_id": event.get("team_application_group_id", ""),
            "travel_support": travel, "application_url": app.get("application_url", ""),
            "form_provider": app.get("form_provider", ""),
        })
    member_order = {member_id: index for index, member_id in enumerate(team.members)}
    queue.sort(key=lambda x: (-_safe_float(x.get("team_apply_score")), x.get("event_name", ""), member_order.get(x.get("applicant_id"), 99)))
    return queue


def run_live_cycle(
    submission_executor: Callable[..., dict] | None = None,
    preflight: bool = False,
    event_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Execute the controlled live application cycle.

    The daily pipeline has already persisted and scored the candidate data in
    the database, so this function intentionally takes no daily-state
    positional argument. ``submission_executor`` is injectable for tests and
    must have the same signature as :func:`execute_live_submission`.
    """
    if submission_executor is None:
        submission_executor = execute_live_submission
    team = load_team()
    queue = build_live_submission_queue()
    if event_ids is not None:
        queue = [entry for entry in queue if entry["event_id"] in event_ids]
    # Luma is deliberately not part of the autonomous final-submit path after
    # its browser-verification/403 evidence. Qualified pairs are packaged for
    # a person, while every other provider keeps its existing autonomous path.
    from hackathon_searcher.human_assist import HUMAN_ASSISTED, prepare_luma_human_assist_from_queue, provider_capability
    human_assist = prepare_luma_human_assist_from_queue(queue)
    queue = [entry for entry in queue if provider_capability(entry.get("form_provider", "")) != HUMAN_ASSISTED]
    print_live_queue(queue)
    report = {
        "queue": queue,
        "human_assist": human_assist,
        "attempted": [], "submitted": 0, "status_unknown": 0, "blocked": 0,
        "deferred": 0, "submitted_by_applicant": {},
        "travel_support_count": 0, "aborted": False, "abort_reason": "",
        "team_partial_submissions": 0,
    }
    live_count = 0
    aborted = False
    deferred_groups: set[str] = set()
    submitted_by_team: dict[str, set[str]] = {}
    for entry in queue:
        team_group_id = str(entry.get("team_application_group_id", ""))
        if team_group_id in deferred_groups:
            continue
        group_size = sum(1 for candidate in queue if candidate.get("team_application_group_id") == team_group_id)
        if live_count + group_size > settings.MAX_LIVE_APPLICATIONS_PER_RUN:
            log_audit(entry["event_id"], "DEFERRED_LIVE_CAP", "team pair would exceed cap")
            report["deferred"] += group_size
            deferred_groups.add(team_group_id)
            continue
        if live_count >= settings.MAX_LIVE_APPLICATIONS_PER_RUN:
            log_audit(entry["event_id"], "DEFERRED_LIVE_CAP", f"{entry['applicant_id']}: cap")
            report["deferred"] += 1
            continue
        if aborted:
            log_audit(entry["event_id"], "DEFERRED_LIVE_CAP", f"{entry['applicant_id']}: aborted")
            report["deferred"] += 1
            continue
        event_id = entry["event_id"]
        applicant_id = entry["applicant_id"]
        profile = profile_manager.get(applicant_id)
        event = get_event_by_id(event_id)
        if not profile or not event:
            continue
        print(f"\n  LIVE SUBMIT [{live_count+1}/{settings.MAX_LIVE_APPLICATIONS_PER_RUN}]")
        print(f"  Event: {entry['event_name'][:60]}")
        print(f"  Applicant: {profile.name}")
        print(f"  Score: event={entry['event_score']:.0f}, fit={entry['fit_score']:.0f}")

        eligibility = validate_live_eligibility(event_id, applicant_id)
        if not eligibility["passes"]:
            print(f"  BLOCKED: {eligibility['blockers']}")
            report["blocked"] += 1
            continue

        existing = get_application(event_id, applicant_id)
        app_url = str(existing.get("application_url", "") if existing else entry["application_url"])
        fields_data = _json_value(existing.get("questions", "[]") if existing else "[]", [])
        answers = _json_value(existing.get("answers", "[]") if existing else "[]", [])
        try:
            fields = [FormField.model_validate(item) for item in fields_data]
        except Exception:
            fields = [FormField(**item) for item in fields_data if isinstance(item, dict)]
        answer_map = {str(item.get("label", "")).lower(): str(item.get("answer", "")) for item in answers}
        for field in fields:
            field.answer = answer_map.get(field.label.lower(), field.answer)

        # Immediately before snapshot: recheck applicant, email, all answers,
        # duplicate state, readiness flags, and LIVE_TEST_MODE.
        if profile.applicant_id and applicant_id not in {profile.applicant_id, profile.applicant_id.split("_")[0]}:
            print("  BLOCKED: Applicant identity mismatch")
            report["blocked"] += 1
            continue
        if not settings.LIVE_TEST_MODE:
            print("  BLOCKED: LIVE_TEST_MODE is no longer active")
            report["blocked"] += 1
            continue
        if not existing or existing.get("status") != "READY_TO_APPLY":
            print("  BLOCKED: READY_TO_APPLY record changed")
            report["blocked"] += 1
            continue
        if not existing.get("duplicate_check_passed") or not has_application(event_id, applicant_id):
            print("  BLOCKED: Duplicate status recheck failed")
            report["blocked"] += 1
            continue
        if not app_url.startswith("http") or not fields or not answers:
            print("  BLOCKED: Stored form data is incomplete")
            report["blocked"] += 1
            continue
        factual_issues = validate_factual_consistency(fields, answers, profile)
        cross_issues = _cross_profile_issues(applicant_id, answers)
        issues = validate_application(fields)
        if factual_issues or cross_issues or issues:
            print(f"  BLOCKED: validation factual={factual_issues} cross={cross_issues} form={issues}")
            report["blocked"] += 1
            continue

        if (existing.get("form_provider", "") if existing else "").lower() == "luma":
            unchanged, structure_error = verify_live_luma_form_structure(event_id, applicant_id, fields)
            if not unchanged:
                update_event(event_id, {"team_status": "TEAM_BLOCKED_FORM"})
                for teammate_id in load_team().members:
                    _update_blocked_application(event_id, teammate_id, "TEAM_BLOCKED_FORM", structure_error)
                report["blocked"] += 2
                report["aborted"] = True
                report["abort_reason"] = structure_error
                aborted = True
                print(f"  BLOCKED: {structure_error}")
                continue

        snapshot_path = create_submission_snapshot(event_id, applicant_id, fields, answers, eligibility)
        print(f"  Snapshot: {snapshot_path}")

        result = submission_executor(event_id, applicant_id, fields, answers, eligibility, snapshot_path)

        entry_result = {
            "event": entry["event_name"], "applicant": profile.name,
            "event_score": entry["event_score"], "fit_score": entry["fit_score"],
            "travel_support": entry["travel_support"], "application_url": app_url,
            "form_provider": existing.get("form_provider", "") if existing else "",
            "pre_submission_validation": "PASS",
            "submit_clicked": result["submit_clicked"],
            "final_status": result["status"],
            "confirmation": result.get("confirmation_text", "")[:300],
            "snapshot_file": snapshot_path,
        }
        report["attempted"].append(entry_result)
        live_count += 1

        if result["status"] == "PREFLIGHT_ONLY":
            report.setdefault("preflight", 0)
            report["preflight"] += 1
            print("  PREFLIGHT_ONLY: Submit not clicked")
        elif result["status"] == "APPLIED":
            report["submitted"] += 1
            report["submitted_by_applicant"][applicant_id] = report["submitted_by_applicant"].get(applicant_id, 0) + 1
            if "CONFIRMED" in entry["travel_support"]:
                report["travel_support_count"] += 1
            submitted_by_team.setdefault(event_id, set()).add(applicant_id)
            if submitted_by_team[event_id] == set(team.members):
                update_event(event_id, {"team_status": "TEAM_APPLIED"})
            print(f"  APPLIED: {result.get('confirmation_text', '')[:100]}")
        elif result["status"] in UNCERTAIN_SUBMISSION_STATUSES:
            report["status_unknown"] += 1
            aborted = True
            report["aborted"] = True
            report["abort_reason"] = f"{result['status']} on {entry['event_name']} for {profile.name}"
            if submitted_by_team.get(event_id):
                update_event(event_id, {"team_status": "TEAM_PARTIAL_SUBMISSION"})
                log_audit(event_id, "TEAM_PARTIAL_SUBMISSION", f"Unknown submission after teammate submitted")
                report["team_partial_submissions"] += 1
            print(f"  {result['status']} - ABORTING")
            log_audit(event_id, "LIVE_RUN_ABORTED", f"UNKNOWN for {applicant_id}")
        else:
            report["blocked"] += 1
            print(f"  {result['status']}: {result.get('error', '')}")
            if submitted_by_team.get(event_id):
                update_event(event_id, {"team_status": "TEAM_PARTIAL_SUBMISSION"})
                log_audit(event_id, "TEAM_PARTIAL_SUBMISSION", f"{applicant_id} failed after teammate submitted")
                report["team_partial_submissions"] += 1
                report["aborted"] = True
                report["abort_reason"] = f"TEAM_PARTIAL_SUBMISSION for {entry['event_name']}"
            aborted = True
    return report


def print_live_queue(queue: list[dict[str, Any]]) -> None:
    """Print every candidate entering the live cycle after scoring."""
    print(f"\n  LIVE QUEUE AFTER STAGE 2 + SCORING ({len(queue)} candidates):")
    for index, candidate in enumerate(queue, start=1):
        print(
            f"  {index}. event={candidate['event_name'][:60]} | "
            f"applicant={candidate['applicant_name']} | "
            f"event_score={candidate['event_score']:.1f} | "
            f"fit_score={candidate['fit_score']:.1f} | "
            f"travel={candidate['travel_support']} | "
            f"url={candidate['application_url']} | "
            f"reason={candidate['reason']}"
        )


def print_live_report(report):
    """Print the controlled live test report."""
    print()
    print("=" * 70)
    print("  CONTROLLED LIVE TEST REPORT")
    print("=" * 70)
    if report.get("queue"):
        print(f"\n  LIVE TEST QUEUE ({len(report['queue'])} candidates):")
        for i, q in enumerate(report["queue"], start=1):
            print(
                f"  {i}. [{q['priority']}] {q['event_name'][:45]} - "
                f"{q['applicant_name']} | event={q['event_score']:.1f} | "
                f"fit={q['fit_score']:.1f} | travel={q['travel_support']} | "
                f"reason={q['reason']}"
            )
    if report.get("human_assist"):
        print("\n  HUMAN-ASSISTED PACKAGES:")
        for item in report["human_assist"]:
            print(f"  {item['event_id']} | {item['paths'].get('event', '')}")
    print(f"\n  --- RESULTS ---")
    for entry in report.get("attempted", []):
        print(f"\n  EVENT:      {entry['event'][:60]}")
        print(f"  APPLICANT:  {entry['applicant']}")
        print(f"  EVENT SCORE:{entry['event_score']:.0f}")
        print(f"  FIT SCORE:  {entry['fit_score']:.0f}")
        print(f"  TRAVEL:     {entry['travel_support']}")
        print(f"  URL:        {entry['application_url'][:70]}")
        print(f"  VALIDATION: {entry['pre_submission_validation']}")
        print(f"  SUBMITTED:  {'YES' if entry['submit_clicked'] else 'NO'}")
        print(f"  STATUS:     {entry['final_status']}")
        if entry.get("confirmation"):
            print(f"  CONFIRM:    {entry['confirmation'][:150]}")
        print(f"  SNAPSHOT:   {entry['snapshot_file']}")
    print(f"\n  --- SUMMARY ---")
    print(f"  Attempted:  {len(report.get('attempted', []))}")
    print(f"  Confirmed:  {report.get('submitted', 0)}")
    print(f"  Unknown:    {report.get('status_unknown', 0)}")
    print(f"  Blocked:    {report.get('blocked', 0)}")
    print(f"  Deferred:   {report.get('deferred', 0)}")
    for applicant_id, count in report.get("submitted_by_applicant", {}).items():
        print(f"  {applicant_id}: {count}")
    print(f"  Travel:     {report.get('travel_support_count', 0)}")
    if report.get("aborted"):
        print(f"\n  LIVE RUN ABORTED: {report['abort_reason']}")
    print(f"\n  LIVE TEST MODE: {settings.LIVE_TEST_MODE}")
    print(f"  DRY_RUN: {settings.DRY_RUN}")
    print("=" * 70)
