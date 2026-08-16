"""Human-assisted application packages for providers that reject automation."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from hackathon_searcher.database import (
    get_all_applications, get_application, get_applications_for_event,
    get_event_by_id, log_audit, update_application, update_event,
)
from hackathon_searcher.dedicated_browser import ensure_bridge, launch_dedicated_chrome
from hackathon_searcher.team import load_team

AUTONOMOUS_SUPPORTED = "AUTONOMOUS_SUPPORTED"
HUMAN_ASSISTED = "HUMAN_ASSISTED"
UNSUPPORTED = "UNSUPPORTED"
HUMAN_ASSISTED_SUBMISSION_REQUIRED = "HUMAN_ASSISTED_SUBMISSION_REQUIRED"

# Provider-level policy. Unknown providers retain the normal autonomous path.
PROVIDER_CAPABILITIES: dict[str, str] = {"luma": HUMAN_ASSISTED}
SELECTION_PATH = Path("human_assist") / "active_selection.json"


def provider_capability(provider: str) -> str:
    return PROVIDER_CAPABILITIES.get((provider or "").lower(), AUTONOMOUS_SUPPORTED)


def provider_capability_table() -> list[tuple[str, str]]:
    return [
        ("luma", HUMAN_ASSISTED),
        ("other providers", AUTONOMOUS_SUPPORTED),
        ("explicitly unsupported providers", UNSUPPORTED),
    ]


def _value(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value if value is not None else fallback


def _package_dir(event: dict) -> Path:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = re.sub(r"[^a-z0-9]+", "_", event.get("event_name", "event").lower()).strip("_")[:60]
    return Path("human_assist") / f"{day}_{slug}"


def _app_text(app: dict) -> str:
    questions = _value(app.get("questions", "[]"), [])
    answers = _value(app.get("answers", "[]"), [])
    answer_by_label = {str(item.get("label", "")).strip().lower(): str(item.get("answer", "")) for item in answers if isinstance(item, dict)}
    lines = [
        f"Applicant: {app.get('applicant_name', '')}",
        f"Email: {next((answer_by_label[k] for k in answer_by_label if 'email' in k), '')}",
        f"Application URL: {app.get('application_url', '')}",
        "",
        "Exact answers (copy exactly):",
    ]
    for question in questions:
        if not isinstance(question, dict):
            continue
        label = str(question.get("label", question.get("name", "")))
        answer = answer_by_label.get(label.strip().lower(), str(question.get("answer", "")))
        lines.extend([f"Question: {label}", f"Answer: {answer}", ""])
    lines.extend([
        "Consent selections are included above where required.",
        f"Special notes: {app.get('notes', '') or 'None'}",
        "After you click Luma's final submit button, the extension records the application when Luma confirms or changes screen. Use `human-assist mark-submitted <event_id>` only as a fallback if that click cannot be verified.",
    ])
    return "\n".join(lines) + "\n"


def create_human_assist_package(event_id: str, *, mark_required: bool = True) -> dict[str, str]:
    """Write a copy-ready package for a fully prepared Luma team application."""
    event = get_event_by_id(event_id)
    apps = get_applications_for_event(event_id)
    if not event or not apps or any((app.get("form_provider", "").lower() != "luma") for app in apps):
        raise ValueError("A complete Luma team application is required")
    directory = _package_dir(event)
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    for app in apps:
        path = directory / f"{app.get('applicant_id', 'applicant')}.txt"
        path.write_text(_app_text(app), encoding="utf-8")
        paths[app.get("applicant_id", "")] = str(path)
    event_path = directory / "event.txt"
    uncertain = any(app.get("status") in {"SUBMISSION_STATUS_UNKNOWN", "SUBMITTED_CONFIRMATION_UNKNOWN"} for app in apps)
    event_path.write_text(
        "\n".join([
            f"Event: {event.get('event_name', '')}",
            f"Luma URL: {event.get('event_url', '') or next((a.get('application_url', '') for a in apps), '')}",
            f"Deadline: {event.get('application_deadline', '') or 'unknown'}",
            f"Team score: {event.get('team_apply_score', 0):.2f}",
            f"Travel status: {event.get('team_travel_status', '') or event.get('final_travel_status', '') or 'unknown'}",
            f"Status: {HUMAN_ASSISTED_SUBMISSION_REQUIRED}",
            "Provider capability: HUMAN_ASSISTED (Luma browser verification must be completed by a person).",
            "WARNING: prior submission evidence is uncertain; verify the existing Luma/email state before manually submitting again." if uncertain else "No prior uncertain submission is recorded.",
        ]) + "\n",
        encoding="utf-8",
    )
    paths["event"] = str(event_path)
    if mark_required:
        update_event(event_id, {"team_status": HUMAN_ASSISTED_SUBMISSION_REQUIRED})
        for app in apps:
            # Do not erase an uncertain/previous submission record; that remains a
            # duplicate-protection signal. Newly ready records become manual tasks.
            if app.get("status") in {"READY_TO_APPLY", "TEAM_PENDING"}:
                update_application(event_id, app["applicant_id"], {"status": HUMAN_ASSISTED_SUBMISSION_REQUIRED})
        log_audit(event_id, HUMAN_ASSISTED_SUBMISSION_REQUIRED, f"Luma package: {directory}")
    return paths


def prepare_luma_human_assist_from_queue(queue: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Divert qualified Luma pairs from autonomous submission to manual packages."""
    event_ids = {entry["event_id"] for entry in queue if provider_capability(entry.get("form_provider", "")) == HUMAN_ASSISTED}
    prepared = []
    for event_id in event_ids:
        try:
            prepared.append({"event_id": event_id, "paths": create_human_assist_package(event_id)})
        except ValueError:
            continue
    return prepared


def pending_human_assist_events() -> list[dict]:
    """Return Luma team events with prepared answers awaiting human confirmation."""
    candidates: dict[str, dict] = {}
    for app in get_all_applications():
        if app.get("form_provider", "").lower() == "luma":
            event = get_event_by_id(app["event_id"])
            if event and event.get("team_status") != "TEAM_APPLIED" and (event.get("team_status") == HUMAN_ASSISTED_SUBMISSION_REQUIRED or event.get("team_apply_score", 0) >= 55):
                candidates[event["event_id"]] = event
    return list(candidates.values())


def mark_manually_submitted(event_id: str) -> None:
    event = get_event_by_id(event_id)
    apps = get_applications_for_event(event_id)
    team = load_team()
    if not event or {app.get("applicant_id") for app in apps} != set(team.members) or any(app.get("form_provider", "").lower() != "luma" for app in apps):
        raise ValueError("Expected prepared Luma applications for every configured team member")
    for app in apps:
        update_application(event_id, app["applicant_id"], {"status": "MANUALLY_SUBMITTED", "notes": "User confirmed manual Luma submission"})
    update_event(event_id, {"team_status": "TEAM_APPLIED"})
    log_audit(event_id, "MANUALLY_SUBMITTED", "User confirmed both team members submitted through Luma")


def _prepared_event(event_id: str) -> tuple[dict, dict[str, str]]:
    """Load one pending Luma team event and ensure its package is current."""
    events = pending_human_assist_events()
    if not events:
        raise ValueError("No prepared Luma applications require manual submission")
    event = get_event_by_id(event_id) if event_id else events[0]
    if not event or event["event_id"] not in {item["event_id"] for item in events}:
        raise ValueError("No prepared Luma application found for that event")
    return event, create_human_assist_package(event["event_id"])


def _app_url(event: dict, app: dict) -> str:
    """Create a tab-bound Luma URL; the extension uses these values as its key."""
    url = event.get("event_url", "") or app.get("application_url", "")
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query.update({
        "hs_event": event["event_id"],
        # The bridge accepts only IDs named in the active local selection.  This
        # keeps every browser tab tied to its own prepared application without
        # embedding person-specific aliases in the public source.
        "hs_applicant": app["applicant_id"],
    })
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _write_active_selection(event_id: str, applicant_ids: list[str]) -> None:
    """Expose only explicit applicant/event combinations to the loopback bridge."""
    SELECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SELECTION_PATH.write_text(
        json.dumps({"event_id": event_id, "applicant_ids": applicant_ids}), encoding="utf-8"
    )


def open_human_assist(event_id: str = "", applicant_id: str = "") -> tuple[dict, dict[str, str], str]:
    """Open one explicitly selected applicant tab (useful for an individual revisit)."""
    event, paths = _prepared_event(event_id)
    if not applicant_id:
        apps = get_applications_for_event(event["event_id"])
        if not apps:
            raise ValueError("Prepared applicant not found")
        applicant_id = apps[0]["applicant_id"]
    app = get_application(event["event_id"], applicant_id)
    if not app:
        raise ValueError("Prepared applicant not found")
    _write_active_selection(event["event_id"], [applicant_id])
    if not ensure_bridge():
        raise RuntimeError("Could not start the local browser bridge on 127.0.0.1")
    selected_url = _app_url(event, app)
    launch_dedicated_chrome([selected_url])
    log_audit(event["event_id"], "HUMAN_ASSIST_AUTOFILL_OPENED", f"{applicant_id}: local Chrome autofill selected", applicant_id=applicant_id)
    return event, paths, selected_url


def open_team_human_assist(event_id: str = "") -> tuple[dict, dict[str, str], dict[str, str]]:
    """Open separately bound tabs for every prepared member of one team event."""
    event, paths = _prepared_event(event_id)
    prepared_apps = get_applications_for_event(event["event_id"])
    team = load_team()
    if {app.get("applicant_id") for app in prepared_apps} != set(team.members):
        raise ValueError("Expected prepared applications for every configured team member")
    applicants = {app["applicant_id"]: app for app in prepared_apps}
    _write_active_selection(event["event_id"], list(applicants))
    if not ensure_bridge():
        raise RuntimeError("Could not start the local browser bridge on 127.0.0.1")
    urls = {applicant_id: _app_url(event, app) for applicant_id, app in applicants.items()}
    # Each URL carries its own applicant ID. The extension asks for that exact
    # pair, so one tab can never receive the other tab's prepared answers.
    launch_dedicated_chrome(list(urls.values()))
    for applicant_id in applicants:
        log_audit(event["event_id"], "HUMAN_ASSIST_AUTOFILL_OPENED", f"{applicant_id}: team Chrome autofill tab opened", applicant_id=applicant_id)
    return event, paths, urls


def open_next_human_assist() -> tuple[dict, dict[str, str], str]:
    """Backwards-compatible single-applicant helper."""
    return open_human_assist()
