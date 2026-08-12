"""
Dashboard module — multi-applicant edition.

Shows events and applications with per-applicant status columns.
"""

import json
from typing import Optional

from hackathon_searcher.database import (
    get_all_events, get_all_applications, get_applications_for_event,
    get_application, get_event_by_id, get_audit_log, get_last_crawl,
)
from hackathon_searcher.profile import profile_manager
from hackathon_searcher.settings import settings


def print_overview() -> None:
    events = get_all_events()
    apps = get_all_applications()
    last_crawl = get_last_crawl()

    total = len(events)
    qualified = sum(1 for e in events if e.get("status") == "QUALIFIED")
    applied = sum(1 for a in apps if a.get("status") in ("APPLIED", "DRY_RUN_COMPLETE"))
    blocked = sum(1 for a in apps if a.get("status", "").startswith("BLOCKED"))
    flight_supported = sum(1 for e in events if e.get("travel_support") in (
        "CONFIRMED_FLIGHTS", "CONFIRMED_TRAVEL_REIMBURSEMENT", "CONFIRMED_TRAVEL_STIPEND"
    ))

    print()
    print("=" * 65)
    print("  HACKATHON SEARCHER — Dashboard")
    print("=" * 65)
    print()
    print(f"  Events:        {total} total")
    print(f"    Qualified:   {qualified}")
    print(f"    Flights:     {flight_supported}")
    print(f"  Applications:  {len(apps)} total")
    print(f"    Completed:   {applied}")
    print(f"    Blocked:     {blocked}")
    print()
    print(f"  Applicants:")
    for pid in profile_manager.applicant_ids:
        p = profile_manager.get(pid)
        p_apps = [a for a in apps if a.get("applicant_id") == pid]
        p_applied = sum(1 for a in p_apps if a.get("status") in ("APPLIED", "DRY_RUN_COMPLETE"))
        print(f"    {p.name if p else pid}: {len(p_apps)} apps ({p_applied} completed)")
    print()
    print(f"  Dry run:       {settings.DRY_RUN}")
    print(f"  Auto apply:    {settings.AUTO_APPLY}")
    print()

    if last_crawl:
        print(f"  Last crawl:    {last_crawl.get('last_crawl', 'Never')}")
        print(f"    Scanned: {last_crawl.get('events_scanned', 0)}, "
              f"New: {last_crawl.get('events_new', 0)}, "
              f"Submitted: {last_crawl.get('applications_submitted', 0)}")
        print()

    # Top events
    print("-" * 65)
    print("  Top Hackathons:")
    print()
    for event in events[:8]:
        name = event.get("event_name", "?")[:35]
        score = event.get("event_score", 0)
        city = event.get("city", "?")
        travel = event.get("travel_support", "Unknown")
        event_apps = get_applications_for_event(event.get("event_id", ""))

        print(f"  [{score:5.1f}] {name}")
        print(f"           {city} | Travel: {travel}")
        if event.get("team_status"):
            scores = event.get("team_member_scores", "{}")
            try: scores = json.loads(scores) if isinstance(scores, str) else scores
            except json.JSONDecodeError: scores = {}
            print(f"           Team: {scores} | avg {event.get('team_apply_score', 0):.1f} | {event['team_status']}")

        if event_apps:
            for a in event_apps:
                status = a.get("status", "?")
                fit = a.get("applicant_fit_score", 0)
                name = a.get("applicant_name", "?")
                print(f"             {name}: {status} (fit: {fit:.0f})")
        print()

    # Applications
    if apps:
        print("-" * 65)
        print("  Applications:")
        print()
        for app in apps[:8]:
            evt_name = app.get("event_name", "?")[:40]
            applicant = app.get("applicant_name", "?")
            status = app.get("status", "?")
            score = app.get("applicant_fit_score", 0)
            print(f"  [{applicant}] {evt_name}")
            print(f"    Status: {status} | Fit: {score:.0f}")
            print()

    print("=" * 65)


def print_events_table(status_filter: Optional[str] = None) -> None:
    events = get_all_events(status_filter)
    applicant_ids = profile_manager.applicant_ids

    header = f"{'Event':<30} {'Loc':<15} {'Score':>5} {'Team avg':>8} {'Team decision':<23}"
    for aid in applicant_ids:
        p = profile_manager.get(aid)
        header += f" {p.name if p else aid:<15}"
    print()
    print(header)
    print("-" * (95 + 17 * len(applicant_ids)))

    for event in events:
        name = event.get("event_name", "?")[:28]
        loc = f"{event.get('city', '?')},{event.get('country', '?')}"[:13]
        score = event.get("event_score", 0)
        row = f"{name:<30} {loc:<15} {score:>5.1f} {event.get('team_apply_score', 0):>8.1f} {event.get('team_status', '-')[:23]:<23}"

        event_apps = get_applications_for_event(event.get("event_id", ""))
        for aid in applicant_ids:
            matching = [a for a in event_apps if a.get("applicant_id") == aid]
            if matching:
                status = matching[0].get("status", "?")
                status_short = {"APPLIED": "APPLIED", "DRY_RUN_COMPLETE": "DRY_OK",
                                "QUALIFIED": "QUAL", "BLOCKED_CAPTCHA": "CAPTCHA",
                                "BLOCKED_UNKNOWN_FIELD": "UNK_FLD",
                                "INELIGIBLE": "INELIG"}.get(status, status[:7])
                row += f" {status_short:<15}"
            else:
                row += f" {'-':<15}"
        print(row)
    print()


def print_event_detail(event_id: str) -> None:
    event = get_event_by_id(event_id)
    if not event:
        print(f"Event {event_id} not found.")
        return

    event_apps = get_applications_for_event(event_id)

    print()
    print("=" * 65)
    print(f"  {event.get('event_name', 'Unknown')}")
    print("=" * 65)
    print()
    print(f"  Organizer:      {event.get('organizer', 'N/A')}")
    print(f"  Location:       {event.get('city', '?')}, {event.get('country', '?')}")
    print(f"  Type:           {event.get('physical_or_online', 'Unknown')}")
    print(f"  Dates:          {event.get('start_date', '?')} - {event.get('end_date', '?')}")
    print(f"  Deadline:       {event.get('application_deadline', 'Unknown')}")
    print()
    print(f"  Event Score:    {event.get('event_score', 0):.1f}/100")
    print(f"  Travel Support: {event.get('travel_support', 'Unknown')}")
    print(f"  Travel Prob:    {event.get('travel_support_probability', 0):.0%}")
    print()

    sponsors_raw = event.get("sponsors", "[]")
    try:
        sponsors = json.loads(sponsors_raw) if isinstance(sponsors_raw, str) else sponsors_raw
    except (json.JSONDecodeError, TypeError):
        sponsors = []
    if sponsors:
        print(f"  Sponsors:       {', '.join(sponsors[:10])}")
        print()

    # Per-applicant details
    if event_apps:
        print(f"  Team decision:  {event.get('team_status', 'not calculated')}")
        print(f"  Team scores:    {event.get('team_member_scores', '{}')} | Average {event.get('team_apply_score', 0):.1f}")
        print("-" * 65)
        for app in event_apps:
            applicant = app.get("applicant_name", "?")
            print(f"  [{applicant}]")
            print(f"    Status:       {app.get('status', '?')}")
            print(f"    Fit Score:    {app.get('applicant_fit_score', 0):.0f}")
            print(f"    Eligibility:  {app.get('eligibility_status', '?')}")
            print(f"    Travel OK:    {'Yes' if app.get('travel_eligible') else 'No'}")
            print()

            answers_raw = app.get("answers", "[]")
            try:
                answers = json.loads(answers_raw) if isinstance(answers_raw, str) else answers_raw
            except (json.JSONDecodeError, TypeError):
                answers = []

            if answers:
                print("    Answers:")
                for a in answers[:10]:
                    label = a.get("label", "?")
                    answer = a.get("answer", "")
                    print(f"      Q: {label}")
                    print(f"      A: {answer[:150]}")
                    print()
        print("-" * 65)

    # Score reasoning
    reasoning = event.get("score_reasoning", "")
    if reasoning:
        print("  Score Breakdown:")
        for line in reasoning.split("\n"):
            print(f"    {line}")
        print()

    print("=" * 65)


def print_daily_report() -> None:
    last_crawl = get_last_crawl()
    if not last_crawl:
        print("No crawl data yet.")
        return

    report_raw = last_crawl.get("report", "{}")
    try:
        report = json.loads(report_raw) if isinstance(report_raw, str) else report_raw
    except (json.JSONDecodeError, TypeError):
        report = {}

    print()
    print("=" * 65)
    print("  DAILY HACKATHON SEARCH — Last Report")
    print("=" * 65)
    print()
    print(f"  Time:          {last_crawl.get('last_crawl', 'Unknown')}")
    print(f"  Scanned:       {last_crawl.get('events_scanned', 0)}")
    print(f"  New:           {last_crawl.get('events_new', 0)}")
    print(f"  Submitted:     {last_crawl.get('applications_submitted', 0)}")
    print(f"  Blocked:       {last_crawl.get('applications_blocked', 0)}")
    print()

    top = report.get("top_applications", [])
    if top:
        print("  Applications:")
        for a in top:
            print(f"    [{a.get('applicant', '?')}] {a.get('event_name', '?')}: "
                  f"Score {a.get('score', 0):.0f}, Status: {a.get('status', '?')}")
        print()

    high = report.get("new_high_score_events", [])
    if high:
        print("  High-Score Events:")
        for ev in high:
            print(f"    {ev.get('name', '?')}: {ev.get('score', 0)}/100 | Travel: {ev.get('travel', '?')}")
        print()

    errors = report.get("errors", [])
    if errors:
        print(f"  Errors ({len(errors)}):")
        for err in errors[:5]:
            print(f"    - {err}")
        print()

    print("=" * 65)


def print_profile_summary() -> None:
    print()
    print("=" * 65)
    print("  PROFILES")
    print("=" * 65)

    for applicant_id in profile_manager.applicant_ids:
        p = profile_manager.get(applicant_id)
        if not p:
            continue
        print()
        print(f"  [{applicant_id}]")
        print(f"    Name:       {p.name}")
        print(f"    Full name:  {p.full_name}")
        print(f"    Age:        {p.age}")
        print(f"    Location:   {p.city}, {p.country}")
        print(f"    Email:      {p.email}")
        print(f"    Student:    {p.current_student}")
        print(f"    Univ:       {p.is_university_student}")
        print(f"    Projects:   {len(p.projects)}")
        print(f"    Interests:  {', '.join(p.interests[:5])}...")
        print()

    print("=" * 65)


def print_settings() -> None:
    print()
    print("=" * 65)
    print("  SETTINGS")
    print("=" * 65)
    print()
    for field_name in settings.__dataclass_fields__:
        if field_name.startswith("_"):
            continue
        value = getattr(settings, field_name, None)
        print(f"  {field_name}: {value}")
    print()
    print("=" * 65)
