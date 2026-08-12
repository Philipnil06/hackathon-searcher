"""
Daily autonomous execution pipeline — improved edition.

Features:
- Adaptive Stage 2 cap (priority events always bypass)
- Improved Stage 1 filter with thematic + applicant pre-fit
- New event priority + deadline urgency scoring
- Stockholm timezone in reports
- Full audit log for every Stage 1 rejection
"""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from hackathon_searcher.database import (
    init_db, get_event_by_id, get_events_needing_stage2_research,
    get_events_with_pending_applications, get_pending_applications,
    has_application, get_application, insert_application, update_application,
    update_event, log_audit,
    start_daily_run, complete_daily_run, fail_daily_run,
    get_last_daily_run, is_daily_run_active,
    acquire_run_lock, release_run_lock,
    get_applications_for_event, create_application_group, get_all_applications,
)
from hackathon_searcher.event_research import fetch_page
from hackathon_searcher.llm import (
    analyze_event_page, generate_application_answer,
    review_application_quality, interpret_eligibility,
    complete,
)
from hackathon_searcher.forms import (
    extract_form_fields, generate_answer_for_field,
    create_submission_snapshot, validate_application,
)
from hackathon_searcher.profile import profile_manager
from hackathon_searcher.scoring import score_event, score_applicant_fit
from hackathon_searcher.scraper import discover_events, process_discovered_events
from hackathon_searcher.settings import settings
from hackathon_searcher.team import load_team
from hackathon_searcher.travel import assess_accommodation, assess_location, assess_travel_support

# === CONSTANTS ===

STAGE2_CAP = 15  # Normal cap for production. Priority events always bypass.

# Keywords that make an event ALWAYS worth Stage 2 research (extremely selective)
MANDATORY_RESEARCH_KEYWORDS = [
    "defense tech", "defence tech", "defense hackathon", "defence hackathon",
]

# Thematic keywords for Stage 1 scoring
THEMATIC_KEYWORDS = {
    "defense": 12, "defence": 12, "robot": 15, "hardware": 14,
    "drone": 15, "autonomy": 15, "computer vision": 14,
    "embedded": 13, "ai agent": 15, "llm": 13, "nlp": 13,
    "developer tool": 13, "devtool": 13, "infrastructure": 13,
    "fintech": 13, "payments": 13, "cyber": 12, "security": 12,
    "edtech": 12, "consumer": 11, "startup": 12, "founder": 12,
    "space": 12, "biotech": 11, "gaming": 9, "health": 10,
}

# Strong sponsor patterns
STRONG_SPONSOR_PATTERNS = [
    "anthropic", "openai", "nvidia", "google", "microsoft", "aws",
    "anduril", "palantir", "y combinator", "sequoia", "a16z",
    "stripe", "vercel", "supabase", "mistral", "hugging face",
    "cursor", "lovable", "elevenlabs", "antler", "accel",
    "entrepreneur first", "index ventures", "general catalyst",
    "founders fund", "boston dynamics", "lockheed",
]

# === TIMEZONE ===

def stockholm_now() -> datetime:
    """Current time in Europe/Stockholm."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Stockholm"))
    except Exception:
        # Fallback: CEST = UTC+2
        return datetime.now(timezone.utc) + timedelta(hours=2)


def stockholm_str(dt: Optional[datetime] = None) -> str:
    """Format a datetime in Stockholm time."""
    dt = dt or stockholm_now()
    return dt.strftime("%Y-%m-%d %H:%M CEST")


# === STAGE 1: IMPROVED FILTER ===

def stage1_score(event: dict) -> tuple[bool, int, str]:
    """
    Improved Stage 1 filter with thematic scoring and applicant pre-fit.

    Returns (should_research, priority_score, reason).
    Higher priority_score = more important to research.
    """
    score = 0
    reasons = []

    # Location comes first. The first configured team member determines cheap
    # discovery priority; final preparation still enforces every member's
    # profile, so no team application can bypass a member-level hard gate.
    primary = _primary_profile()
    if primary:
        location = assess_location(event, primary)
        if not location["allowed"]:
            return False, 0, f"location:{location['status']}"
        score += int(location["fit"] * 3)
        reasons.append(f"location:{location['status']}")

        travel = assess_travel_support(event, primary)
        accommodation = assess_accommodation(event, primary)
        # Reject only deterministic failures. Unknown required support is
        # researched, then remains blocked until verified before application.
        if travel["status"] in {"UNSUPPORTED_REQUIRED", "BELOW_MINIMUM"}:
            return False, 0, f"travel:{travel['status']}"
        if not accommodation["meets_requirement"]:
            return False, 0, f"accommodation:{accommodation['status']}"
        if travel["useful"]:
            score += 10
            reasons.append(f"travel:{travel['status']}")
        elif travel["status"] == "UNKNOWN_REQUIRED":
            reasons.append("travel:research_required")

    # === Thematic scoring ===
    name_desc = (event.get("event_name", "") + " " + (event.get("description", "") or "")).lower()
    themes_raw = event.get("themes", "[]")
    try:
        themes = json.loads(themes_raw) if isinstance(themes_raw, str) else themes_raw
    except (json.JSONDecodeError, TypeError):
        themes = []

    combined_text = name_desc + " " + " ".join(themes if isinstance(themes, list) else [])

    theme_score = 0
    matched_themes = []
    for kw, pts in THEMATIC_KEYWORDS.items():
        if kw in combined_text:
            theme_score = max(theme_score, pts)
            matched_themes.append(kw)

    score += theme_score
    if matched_themes:
        reasons.append(f"themes:{','.join(matched_themes[:3])}")

    # Mandatory research keywords
    for kw in MANDATORY_RESEARCH_KEYWORDS:
        if kw in combined_text:
            score += 20
            reasons.append(f"mandatory:{kw}")
            break  # Only count once

    # === Sponsor quality ===
    sponsors_raw = event.get("sponsors", "[]")
    try:
        sponsors = json.loads(sponsors_raw) if isinstance(sponsors_raw, str) else sponsors_raw
    except (json.JSONDecodeError, TypeError):
        sponsors = []

    if isinstance(sponsors, list):
        for sponsor in sponsors:
            s_lower = sponsor.lower() if isinstance(sponsor, str) else ""
            for pat in STRONG_SPONSOR_PATTERNS:
                if pat in s_lower:
                    score += 25
                    reasons.append(f"sponsor:{sponsor}")
                    break

    # === Hardware/mentoring bonuses ===
    extra_raw = event.get("extra_data", "{}")
    try:
        extra = json.loads(extra_raw) if isinstance(extra_raw, str) else extra_raw
    except (json.JSONDecodeError, TypeError):
        extra = {}

    if extra.get("on_site_hardware_provided"):
        score += 15
        reasons.append("hardware")
    if extra.get("mentoring_available"):
        score += 5

    # === Applicant pre-fit (cheap, deterministic) ===
    pre_fits = {applicant_id: _cheap_pre_fit(event, applicant_id, combined_text) for applicant_id in profile_manager.applicant_ids}
    best_pre_fit = max(pre_fits.values(), default=0)

    score += best_pre_fit
    for applicant_id, pre_fit in pre_fits.items():
        if pre_fit >= 15:
            reasons.append(f"{applicant_id}_fit={pre_fit}")

    # === Decision ===
    should = score >= 20  # Minimum threshold
    return should, score, "; ".join(reasons) if reasons else "no_signals"


def _cheap_pre_fit(event: dict, applicant_id: str, combined_text: str) -> int:
    """Cheap deterministic pre-fit score for one applicant. Returns 0-30."""
    score = 0
    profile = profile_manager.get(applicant_id)
    if not profile:
        return 0
    location = assess_location(event, profile)
    if not location["allowed"]:
        return 0
    interests = {str(item).lower() for item in (profile.interests if profile else [])}
    skills = {str(item).lower() for item in (profile.skills if profile else [])}
    for keyword in interests | skills:
        if keyword and keyword in combined_text:
            score += 10
            break

    # General interest overlap
    for interest in interests:
        if any(part in combined_text for part in interest.split()):
            score += 3
            break

    # Location is deterministic and outweighs a coincidental keyword match at
    # this inexpensive filtering stage.
    score += int(location["fit"] * 0.5)

    return min(score, 30)


def _primary_profile():
    """Return the profile used for deterministic discovery prioritization."""
    try:
        team = load_team()
        if team.members:
            profile = profile_manager.get(team.members[0])
            if profile:
                return profile
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return profile_manager.all_profiles[0] if profile_manager.all_profiles else None


# === DEADLINE URGENCY ===

def deadline_urgency(event: dict) -> tuple[int, str]:
    """Calculate urgency based on application deadline. Returns (urgency_score, label)."""
    deadline = event.get("application_deadline", "")
    if not deadline:
        return 0, ""

    try:
        dl_date = datetime.strptime(str(deadline)[:10], "%Y-%m-%d").date()
        today = stockholm_now().date()
        days_remaining = (dl_date - today).days

        if days_remaining <= 0:
            return 0, "CLOSED"
        elif days_remaining <= 2:
            return 100, "CRITICAL"
        elif days_remaining <= 7:
            return 70, "HIGH"
        elif days_remaining <= 14:
            return 40, "MEDIUM"
        elif days_remaining <= 30:
            return 15, "LOW"
        else:
            return 0, ""
    except (ValueError, TypeError):
        return 0, ""


# === EVENT PRIORITY RANKING ===

def rank_events_for_research(events: list[dict]) -> list[dict]:
    """
    Rank only location-compatible Stage 1 candidates for Stage 2 research.
    Newness, deadline and deterministic profile fit decide the order.
    """
    scored = []
    for event in events:
        should, priority_score, reason = stage1_score(event)
        if not should:
            continue

        # Boost for NEW events
        is_new = event.get("status") == "DISCOVERED"
        is_updated = event.get("status") == "UPDATED"

        if is_new:
            priority_score += 30
        elif is_updated:
            priority_score += 15

        # Deadline urgency
        urgency, urgency_label = deadline_urgency(event)
        priority_score += urgency

        scored.append((event, priority_score, reason, urgency_label))

    # Sort by priority (descending), then by deadline urgency
    scored.sort(key=lambda x: (x[1], x[3] == "CRITICAL"), reverse=True)
    return [e for e, _, _, _ in scored]


def is_mandatory_research(event: dict) -> bool:
    """Check if an event MUST be researched regardless of cap. Keep tight."""
    combined = (event.get("event_name", "") + " " + (event.get("description", "") or "")).lower()
    for kw in MANDATORY_RESEARCH_KEYWORDS:
        if kw in combined:
            return True

    return False


# === STAGE 2 RESEARCH ===

def stage2_research_event(event_id: str) -> Optional[dict]:
    """Expensive external research for a single event."""
    event = get_event_by_id(event_id)
    if not event:
        return None

    url = event.get("event_url", "")
    if not url or not url.startswith("http"):
        log_audit(event_id, "STAGE2_SKIP", "No valid external URL")
        return None

    print(f"  [Stage 2] Researching: {event.get('event_name', '')[:60]}")

    html = fetch_page(url)
    if not html or len(html) < 1000:
        log_audit(event_id, "STAGE2_FETCH_FAIL", "Could not fetch external page")
        return None

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(separator=" ", strip=True)[:8000]

    try:
        analysis = analyze_event_page(text, event.get("event_name", ""))
    except Exception as e:
        log_audit(event_id, "STAGE2_LLM_FAIL", str(e)[:200])
        return None

    if not analysis:
        return None

    findings = {}
    current_ts = event.get("travel_support", "")
    new_ts = analysis.get("travel_support_status", "")

    if new_ts and "CONFIRMED" in new_ts:
        findings["travel_support"] = new_ts
        findings["travel_support_confidence"] = analysis.get("confidence", 0.8)
    elif new_ts and "CONFIRMED" not in current_ts:
        findings["travel_support"] = new_ts
        findings["travel_support_confidence"] = analysis.get("confidence", 0.7)

    if analysis.get("themes"):
        findings["themes"] = json.dumps(analysis["themes"])
    if analysis.get("sponsors"):
        findings["sponsors"] = json.dumps(analysis["sponsors"])
    if analysis.get("eligibility_requirements"):
        findings["eligibility_rules_raw"] = analysis["eligibility_requirements"]
    if analysis.get("travel_support_details"):
        findings["travel_support_details"] = json.dumps({
            "llm_extracted": analysis["travel_support_details"],
            "amount": analysis.get("travel_support_amount", ""),
            "currency": analysis.get("travel_support_currency", ""),
            "conditions": analysis.get("travel_support_conditions", ""),
        })
    if analysis.get("application_deadline"):
        findings["application_deadline"] = analysis["application_deadline"]

    if findings:
        try:
            update_event(event_id, findings)
            log_audit(event_id, "STAGE2_COMPLETE", f"Updated: {list(findings.keys())}")
        except Exception as e:
            log_audit(event_id, "STAGE2_UPDATE_FAIL", str(e)[:200])

    return findings


# === APPLICATION PROCESSING ===

def process_daily_applications(event_id: str) -> dict:
    """Process applications for both applicants for a qualified event."""
    event = get_event_by_id(event_id)
    if not event or event.get("status") == "SCHEDULE_CONFLICT":
        return {"submitted_by_applicant": {}, "blocked": 0}

    result = {"submitted_by_applicant": {}, "blocked": 0}
    app_url = event.get("application_url", event.get("event_url", ""))

    for applicant_id in profile_manager.applicant_ids:
        profile = profile_manager.get(applicant_id)
        if not profile:
            continue

        existing = get_application(event_id, applicant_id)
        if existing and existing.get("status") not in ("QUALIFIED", "READY_TO_APPLY"):
            continue

        fit = score_applicant_fit(event_id, applicant_id)
        if not fit.get("should_apply") or not fit.get("eligible"):
            if existing:
                status = "SKIPPED" if fit.get("should_apply") == False else "INELIGIBLE"
                update_application(event_id, applicant_id, {
                    "status": status,
                    "eligibility_reasoning": fit.get("eligibility_reasoning", ""),
                })
                log_audit(event_id, f"SKIPPED_{status}", f"{applicant_id}: {fit.get('eligibility_reasoning', fit.get('apply_reason', ''))}")
            continue

        if not existing:
            insert_application({
                "application_id": f"app_{event_id[:12]}_{applicant_id}",
                "event_id": event_id, "applicant_id": applicant_id,
                "applicant_name": profile.name,
                "application_url": app_url,
                "date_started": datetime.now(timezone.utc).isoformat(),
                "status": "READY_TO_APPLY",
                "event_score": event.get("event_score", 0),
                "applicant_fit_score": fit.get("fit_score", 0),
                "eligibility_status": "ELIGIBLE",
                "eligibility_reasoning": fit.get("eligibility_reasoning", ""),
                "travel_eligible": 1 if fit.get("travel_eligible") else 0,
                "travel_support_requested": 1,
            })
        else:
            update_application(event_id, applicant_id, {
                "status": "READY_TO_APPLY",
                "applicant_fit_score": fit.get("fit_score", 0),
            })

        submit_result = _generate_and_submit(event, profile, app_url)

        if submit_result.get("status") in ("APPLIED", "DRY_RUN_COMPLETE"):
            result["submitted_by_applicant"][applicant_id] = result["submitted_by_applicant"].get(applicant_id, 0) + 1
        else:
            result["blocked"] = result.get("blocked", 0) + 1

    _maybe_create_group(event_id)
    return result


def _generate_and_submit(event: dict, profile, app_url: str) -> dict:
    """Generate answers and simulate/execute submission."""
    event_id = event["event_id"]
    applicant_id = profile.applicant_id

    if not app_url or not app_url.startswith("http"):
        update_application(event_id, applicant_id, {"status": "BLOCKED_LOGIN", "notes": "No valid URL"})
        log_audit(event_id, "BLOCKED_NO_URL", f"{applicant_id}", applicant_id=applicant_id)
        return {"status": "BLOCKED_LOGIN"}

    log_audit(event_id, "APPLYING", f"{applicant_id}", applicant_id=applicant_id)

    themes_raw = event.get("themes", "[]")
    try:
        themes = json.loads(themes_raw) if isinstance(themes_raw, str) else themes_raw
    except (json.JSONDecodeError, TypeError):
        themes = []

    event_context = {
        "event_name": event.get("event_name", ""),
        "themes": themes,
        "travel_support": event.get("travel_support", ""),
        "organizer": event.get("organizer", ""),
        "description": (event.get("description", "") or "")[:500],
        "city": event.get("city", ""),
        "country": event.get("country", ""),
    }

    # Try fetching form fields
    fields = []
    try:
        html = fetch_page(app_url)
        if html:
            fields = extract_form_fields(html, app_url)
    except Exception:
        pass

    if not fields:
        try:
            from hackathon_searcher.browser import BrowserSession
            with BrowserSession() as browser:
                if browser.navigate(app_url):
                    html = browser.get_page_html()
                    fields = extract_form_fields(html, app_url)
        except Exception:
            pass

    unknown_required = []
    answers = []
    for field in fields:
        answer = generate_answer_for_field(field, profile, event_context, use_llm=True)
        if answer == "UNKNOWN_REQUIRED_FIELD":
            unknown_required.append(field.label)
        answers.append({"label": field.label, "answer": answer})

    if unknown_required:
        update_application(event_id, applicant_id, {
            "status": "BLOCKED_UNKNOWN_FIELD",
            "notes": f"Unknown: {unknown_required}",
            "questions": json.dumps([{"label": f.label} for f in fields]),
            "answers": json.dumps(answers),
        })
        log_audit(event_id, "BLOCKED_UNKNOWN_FIELD", str(unknown_required), applicant_id=applicant_id)
        return {"status": "BLOCKED_UNKNOWN_FIELD"}

    issues = validate_application(fields)
    if issues:
        print(f"  [{applicant_id}] Issues: {issues[:3]}")

    submit_result = {"status": "DRY_RUN_COMPLETE", "confirmation_text": ""}

    if not settings.DRY_RUN and settings.AUTO_APPLY:
        try:
            from hackathon_searcher.browser import fill_application_form
            submit_result = fill_application_form(app_url, fields, profile, dry_run=False)
        except Exception as e:
            submit_result = {"status": "BLOCKED_LOGIN", "error": str(e)[:200]}
    else:
        try:
            from hackathon_searcher.browser import fill_application_form
            submit_result = fill_application_form(app_url, fields, profile, dry_run=True)
        except Exception:
            pass

    now = datetime.now(timezone.utc).isoformat()
    update_application(event_id, applicant_id, {
        "application_url": app_url,
        "questions": json.dumps([{"label": f.label, "type": f.field_type} for f in fields]),
        "answers": json.dumps(answers),
        "date_submitted": now if submit_result["status"] == "APPLIED" else "",
        "application_confirmation": submit_result.get("confirmation_text", ""),
        "status": submit_result["status"],
        "notes": submit_result.get("error", ""),
    })

    log_audit(event_id, "APPLICATION_DONE", f"{applicant_id}: {submit_result['status']}", applicant_id=applicant_id)
    return submit_result


# === MAIN DAILY PIPELINE ===

def run_current_data_preflight() -> dict:
    """Validate the persisted live queue without repeating external research.

    This is the safe implementation behind the ``preflight`` CLI command.
    It runs the complete READY_TO_APPLY and live-cycle validation path with a
    submission-disabled executor, but deliberately reuses the already
    researched/scored database state.  A scheduled ``daily`` run remains the
    only command that refreshes discovery and Stage 2 research.
    """
    # A first-time user has no database yet. Initialize it before checking the
    # database-backed run state so preflight remains a safe first command.
    init_db()
    if not acquire_run_lock():
        print("SKIPPED_ALREADY_RUNNING")
        return {"status": "SKIPPED_ALREADY_RUNNING"}
    try:
        if is_daily_run_active():
            print("SKIPPED_ALREADY_RUNNING: DB lock")
            release_run_lock()
            return {"status": "SKIPPED_ALREADY_RUNNING"}
    except Exception:
        # Never strand the filesystem lock when a damaged or legacy database
        # prevents the early state check from running.
        release_run_lock()
        raise

    run_id = start_daily_run()
    state: dict = {
        "events_total": 0, "events_new": 0, "events_updated": 0,
        "events_researched": 0, "submitted_by_applicant": {}, "applications_blocked": 0,
        "travel_support_found": 0, "errors": [],
    }
    try:
        print("=" * 60)
        print("PREFLIGHT: reusing current researched/scored application data")
        print("DRY_RUN: True — submission executor is disabled")
        print("=" * 60)
        from hackathon_searcher.live_submit import (
            prepare_ready_to_apply, run_live_cycle, print_live_report,
            submission_disabled_executor,
        )
        # Do not create fresh READY_TO_APPLY records from stale discovery data.
        # A preflight reuses only candidates whose real browser form validation
        # explicitly completed without a final submission.
        validated_event_ids = {
            app["event_id"] for app in get_all_applications()
            if app.get("form_validation_status") == "VALIDATED"
        }
        ready_queue = prepare_ready_to_apply(preflight=True, event_ids=validated_event_ids) if validated_event_ids else []
        state["ready_to_apply"] = ready_queue
        live_report = run_live_cycle(
            submission_executor=submission_disabled_executor,
            preflight=True,
        )
        print_live_report(live_report)
        state["submitted_by_applicant"] = live_report.get("submitted_by_applicant", {})
        state["applications_blocked"] = live_report.get("blocked", 0)
        state["live_report"] = live_report
        state["report_path"] = _write_daily_report(state)
        complete_daily_run(run_id, state)
        return state
    except Exception as exc:
        fail_daily_run(run_id, str(exc))
        raise
    finally:
        release_run_lock()

def run_daily_pipeline(dry_run_override: Optional[bool] = None, live_preflight: bool = False) -> dict:
    """Run the efficient daily pipeline with adaptive Stage 2 cap."""
    if dry_run_override is not None:
        original_dry_run = settings.DRY_RUN
        settings.DRY_RUN = dry_run_override

    if not acquire_run_lock():
        print("SKIPPED_ALREADY_RUNNING")
        return {"status": "SKIPPED_ALREADY_RUNNING"}

    if is_daily_run_active():
        print("SKIPPED_ALREADY_RUNNING: DB lock")
        release_run_lock()
        return {"status": "SKIPPED_ALREADY_RUNNING"}

    init_db()
    run_id = start_daily_run()
    errors = []
    state = {
        "events_total": 0, "events_new": 0, "events_updated": 0,
        "events_researched": 0, "submitted_by_applicant": {}, "applications_blocked": 0,
        "travel_support_found": 0, "errors": [],
    }

    try:
        print("=" * 60)
        print(f"DAILY HACKATHON SEARCH — {stockholm_str()}")
        print(f"DRY_RUN: {settings.DRY_RUN}")
        print("=" * 60)

        # === 1. Discover ===
        print("\n[1/6] DISCOVERING EVENTS...")
        raw_events = discover_events()
        state["events_total"] = len(raw_events)
        print(f"  {len(raw_events)} events")

        new_ids, updated_ids, skipped = process_discovered_events(raw_events)
        state["events_new"] = len(new_ids)
        state["events_updated"] = len(updated_ids)
        print(f"  New: {len(new_ids)}, Updated: {len(updated_ids)}, Skipped: {skipped}")

        # === 2. Stage 1 Filter + Rank ===
        print("\n[2/6] STAGE 1 FILTER + RANKING...")
        candidates = get_events_needing_stage2_research()
        ranked = rank_events_for_research(candidates)

        # Log ALL rejections
        for event in candidates:
            should, score, reason = stage1_score(event)
            if not should:
                log_audit(event["event_id"], "SKIPPED_LOW_SCORE",
                          f"score={score}: {reason}")

        # Split mandatory vs optional
        mandatory = [e for e in ranked if is_mandatory_research(e)]
        optional = [e for e in ranked if not is_mandatory_research(e)]

        # Adaptive cap: mandatory events always get researched
        remaining_slots = max(0, STAGE2_CAP - len(mandatory))
        to_research = mandatory + optional[:remaining_slots]

        print(f"  Candidates: {len(candidates)}, Mandatory: {len(mandatory)}, "
              f"Optional slots: {remaining_slots}, Total to research: {len(to_research)}")

        # === 3. Stage 2 Research ===
        print("\n[3/6] STAGE 2 RESEARCH...")
        researched = 0
        for event in to_research:
            try:
                findings = stage2_research_event(event["event_id"])
                if findings:
                    researched += 1
                    if "CONFIRMED" in findings.get("travel_support", ""):
                        state["travel_support_found"] += 1
            except Exception as e:
                errors.append(f"Research failed for {event.get('event_id')}: {e}")
        state["events_researched"] = researched
        print(f"  Researched: {researched}, Travel support found: {state['travel_support_found']}")

        # === 4. Score ===
        print("\n[4/6] SCORING...")
        from hackathon_searcher.database import get_events_needing_research
        to_score = get_events_needing_research()
        for event in to_score[:50]:  # Cap scoring too
            try:
                score_event(event["event_id"])
                for aid in profile_manager.applicant_ids:
                    score_applicant_fit(event["event_id"], aid)
            except Exception as e:
                errors.append(f"Scoring failed: {e}")

        # === 5. Applications ===
        print("\n[5/6] PREPARING APPLICATIONS...")

        if settings.LIVE_TEST_MODE and (not settings.DRY_RUN or live_preflight):
            # CONTROLLED LIVE MODE
            if live_preflight:
                print("  PREFLIGHT MODE ACTIVE — submissions disabled")
            else:
                print("  ⚠️ LIVE MODE ACTIVE — real submissions possible")
            from hackathon_searcher.live_submit import (
                prepare_ready_to_apply, run_live_cycle, print_live_report,
                submission_disabled_executor,
            )
            ready_queue = prepare_ready_to_apply(preflight=live_preflight)
            state["ready_to_apply"] = ready_queue
            live_report = run_live_cycle(
                submission_executor=submission_disabled_executor if live_preflight else None,
                preflight=live_preflight,
            )
            print_live_report(live_report)
            state["submitted_by_applicant"] = live_report.get("submitted_by_applicant", {})
            state["applications_blocked"] = live_report.get("blocked", 0)
            state["live_report"] = live_report
            if live_report.get("aborted"):
                print("\n  ⚠️ LIVE RUN ABORTED — stopping further submissions")
        else:
            # DRY RUN or full auto mode
            # Luma remains fully automated through validation and answer
            # preparation, then becomes a human-assisted package. This does
            # not stop other providers from following their normal path.
            from hackathon_searcher.live_submit import prepare_ready_to_apply
            from hackathon_searcher.human_assist import prepare_luma_human_assist_from_queue
            prepared_queue = prepare_ready_to_apply(preflight=True)
            human_assist = prepare_luma_human_assist_from_queue(prepared_queue)
            state["human_assist"] = human_assist
            if human_assist:
                print(f"  Human-assisted Luma packages: {len(human_assist)}")
            pending_events = get_events_with_pending_applications()
            print(f"  Pending: {len(pending_events)}")
            for event in pending_events:
                try:
                    result = process_daily_applications(event["event_id"])
                    for applicant_id, count in result.get("submitted_by_applicant", {}).items():
                        state["submitted_by_applicant"][applicant_id] = state["submitted_by_applicant"].get(applicant_id, 0) + count
                    state["applications_blocked"] += result.get("blocked", 0)
                except Exception as e:
                    errors.append(f"App failed for {event.get('event_id')}: {e}")

        # === 6. Report ===
        print("\n[6/6] DAILY REPORT...")
        report_path = _write_daily_report(state)
        state["report_path"] = report_path
        state["errors"] = errors

        complete_daily_run(run_id, state)
        print(f"\n{'='*60}")
        print("DAILY RUN COMPLETE")
        print(f"  New: {state['events_new']}, Researched: {state['events_researched']}")
        print(f"  Submitted: {state['submitted_by_applicant']}")
        print(f"  Travel support: {state['travel_support_found']}")
        print("=" * 60)

    except Exception as e:
        errors.append(f"Fatal: {e}")
        try:
            fail_daily_run(run_id, str(e))
        except Exception:
            pass
        print(f"\nDAILY RUN FAILED: {e}")

    finally:
        release_run_lock()
        if dry_run_override is not None:
            settings.DRY_RUN = original_dry_run

    return state


def _write_daily_report(state: dict) -> str:
    """Write daily report with Stockholm timestamps."""
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    now = stockholm_now()
    today = now.strftime("%Y-%m-%d")
    path = reports_dir / f"{today}.txt"
    lines = [
        "DAILY HACKATHON SEARCH",
        now.strftime("%Y-%m-%d %H:%M CEST"), "",
        f"Hackathon Hub events checked: {state.get('events_total', 0)}",
        f"New events: {state.get('events_new', 0)}",
        f"Updated events: {state.get('events_updated', 0)}",
        f"Promising events researched: {state.get('events_researched', 0)}",
        f"Applications by applicant: {state.get('submitted_by_applicant', {})}",
        f"Blocked: {state.get('applications_blocked', 0)}",
        f"Travel support found: {state.get('travel_support_found', 0)}", "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def _maybe_create_group(event_id: str) -> None:
    apps = get_applications_for_event(event_id)
    aids = [a.get("applicant_id") for a in apps if a.get("applicant_id")]
    if len(aids) >= 2:
        group_id = f"grp_{event_id[:16]}_{'_'.join(sorted(aids)[:2])}"
        create_application_group(group_id, event_id, aids)
        for app in apps:
            update_application(app["event_id"], app["applicant_id"], {"application_group_id": group_id})
