"""
Dashboard module.

Provides a human-readable interface for:
- Overview of events and applications
- Searchable event table
- Application detail views
- Profile management
- Settings display
"""

import json
from datetime import datetime, timezone
from typing import Optional

from hackathon_searcher.database import (
    get_all_events,
    get_all_applications,
    get_application,
    get_event_by_id,
    get_audit_log,
    get_last_crawl,
)
from hackathon_searcher.profile import profile
from hackathon_searcher.settings import settings


def print_overview() -> None:
    """Print a concise overview of the current state."""
    events = get_all_events()
    applications = get_all_applications()
    last_crawl = get_last_crawl()

    # Stats
    total_events = len(events)
    qualified = sum(1 for e in events if e.get("status") == "QUALIFIED")
    applied = sum(1 for e in events if e.get("status") == "APPLIED")
    skipped = sum(1 for e in events if e.get("status") == "SKIPPED")
    blocked = sum(1 for e in events if e.get("status", "").startswith("BLOCKED"))
    flight_supported = sum(
        1 for e in events
        if e.get("travel_support", "") in (
            "CONFIRMED_FLIGHTS", "CONFIRMED_TRAVEL_REIMBURSEMENT",
            "CONFIRMED_TRAVEL_STIPEND"
        )
    )

    print()
    print("=" * 60)
    print("  HACKATHON SEARCHER — Dashboard")
    print("=" * 60)
    print()
    print(f"  Events:       {total_events} total")
    print(f"    Qualified:  {qualified}")
    print(f"    Applied:    {applied}")
    print(f"    Skipped:    {skipped}")
    print(f"    Blocked:    {blocked}")
    print(f"    Flights:    {flight_supported}")
    print()
    print(f"  Applications: {len(applications)}")
    print(f"  Profile:      {profile.name} ({profile.city}, {profile.country})")
    print(f"  Auto apply:   {settings.AUTO_APPLY}")
    print(f"  Dry run:      {settings.DRY_RUN}")
    print()

    if last_crawl:
        print(f"  Last crawl:   {last_crawl.get('last_crawl', 'Never')}")
        print(f"    Scanned: {last_crawl.get('events_scanned', 0)}, "
              f"New: {last_crawl.get('events_new', 0)}, "
              f"Submitted: {last_crawl.get('applications_submitted', 0)}")
        print()

    # Top events by score
    print("-" * 60)
    print("  Top Hackathons (by score):")
    print()
    for event in events[:10]:
        name = event.get("event_name", "Unknown")[:40]
        score = event.get("score", 0)
        city = event.get("city", "?")
        country = event.get("country", "?")
        travel = event.get("travel_support", "Unknown")
        status = event.get("status", "?")
        print(f"  [{score:5.1f}] {name}")
        print(f"           {city}, {country} | Travel: {travel} | {status}")
        print()

    # Recent applications
    if applications:
        print("-" * 60)
        print("  Recent Applications:")
        print()
        for app in applications[:5]:
            name = app.get("event_name", "Unknown")[:50]
            status = app.get("status", "?")
            date = app.get("date_applied", "Not yet")
            print(f"  {name}")
            print(f"    Status: {status} | Date: {date}")
            print()

    print("=" * 60)


def print_events_table(status_filter: Optional[str] = None) -> None:
    """Print a table of all events."""
    events = get_all_events(status_filter)

    print()
    print(f"{'Name':<35} {'Location':<20} {'Score':>6} {'Travel':<25} {'Status':<15}")
    print("-" * 105)

    for event in events:
        name = event.get("event_name", "?")[:33]
        location = f"{event.get('city', '?')}, {event.get('country', '?')}"[:18]
        score = event.get("score", 0)
        travel = event.get("travel_support", "?")[:23]
        status = event.get("status", "?")[:13]
        print(f"{name:<35} {location:<20} {score:>6.1f} {travel:<25} {status:<15}")

    print()


def print_event_detail(event_id: str) -> None:
    """Print detailed information about a single event."""
    event = get_event_by_id(event_id)
    if not event:
        print(f"Event {event_id} not found.")
        return

    app = get_application(event_id)

    print()
    print("=" * 60)
    print(f"  {event.get('event_name', 'Unknown')}")
    print("=" * 60)
    print()
    print(f"  Organizer:      {event.get('organizer', 'N/A')}")
    print(f"  Location:       {event.get('city', '?')}, {event.get('country', '?')}")
    print(f"  Venue:          {event.get('venue', 'N/A')}")
    print(f"  Type:           {event.get('physical_or_online', 'Unknown')}")
    print(f"  Dates:          {event.get('start_date', '?')} — {event.get('end_date', '?')}")
    print(f"  Deadline:       {event.get('application_deadline', 'Unknown')}")
    print()
    print(f"  Score:          {event.get('score', 0):.1f}/100")
    print(f"  Status:         {event.get('status', 'Unknown')}")
    print()
    print(f"  Travel Support: {event.get('travel_support', 'Unknown')}")
    print(f"  Travel Type:    {event.get('travel_support_type', 'N/A')}")
    print(f"  Travel Amount:  {event.get('travel_support_amount', 'N/A')} {event.get('travel_support_currency', '')}")
    print(f"  Flights:        {event.get('flight_credits', 'N/A')}")
    print(f"  Accommodation:  {event.get('accommodation', 'N/A')}")
    print(f"  Food:           {event.get('food', 'N/A')}")
    print()

    # Sponsors
    sponsors_raw = event.get("sponsors", "[]")
    try:
        sponsors = json.loads(sponsors_raw) if isinstance(sponsors_raw, str) else sponsors_raw
    except (json.JSONDecodeError, TypeError):
        sponsors = []
    if sponsors:
        print(f"  Sponsors:       {', '.join(sponsors[:10])}")
        print()

    # Description
    desc = event.get("description", "")
    if desc:
        print(f"  Description:    {desc[:300]}...")
        print()

    # Reasoning
    reasoning = event.get("score_reasoning", "")
    if reasoning:
        print(f"  Score Reasoning:")
        for line in reasoning.split("\n"):
            print(f"    {line}")
        print()

    # Application detail
    if app:
        print("-" * 60)
        print("  APPLICATION DETAIL")
        print()
        print(f"  Status:         {app.get('status', 'Unknown')}")
        print(f"  Date Applied:   {app.get('date_applied', 'Not yet')}")
        print(f"  Confirmation:   {app.get('application_confirmation', 'None')[:200]}")
        print()

        # Answers
        answers_raw = app.get("answers", "[]")
        try:
            answers = json.loads(answers_raw) if isinstance(answers_raw, str) else answers_raw
        except (json.JSONDecodeError, TypeError):
            answers = []

        if answers:
            print("  Submitted Answers:")
            for a in answers:
                label = a.get("label", "?")
                answer = a.get("answer", "")
                source = a.get("source", "")
                print(f"    Q: {label}")
                print(f"    A: {answer}")
                if source:
                    print(f"    Source: {source}")
                print()

    # Audit log
    audit = get_audit_log(event_id, limit=10)
    if audit:
        print("-" * 60)
        print("  RECENT AUDIT LOG")
        print()
        for entry in audit:
            ts = entry.get("timestamp", "")[:19]
            action = entry.get("action", "")
            detail = entry.get("detail", "")[:100]
            print(f"  [{ts}] {action}: {detail}")
        print()

    print("=" * 60)


def print_daily_report() -> None:
    """Print the most recent daily report."""
    last_crawl = get_last_crawl()
    if not last_crawl:
        print("No crawl data yet. Run the pipeline first.")
        return

    report_raw = last_crawl.get("report", "{}")
    try:
        report = json.loads(report_raw) if isinstance(report_raw, str) else report_raw
    except (json.JSONDecodeError, TypeError):
        report = {}

    print()
    print("=" * 60)
    print("  DAILY HACKATHON SEARCH — Last Report")
    print("=" * 60)
    print()
    print(f"  Time:            {last_crawl.get('last_crawl', 'Unknown')}")
    print(f"  Scanned:         {last_crawl.get('events_scanned', 0)} events")
    print(f"  New:             {last_crawl.get('events_new', 0)}")
    print(f"  Updated:         {last_crawl.get('events_updated', 0)}")
    print(f"  Submitted:       {last_crawl.get('applications_submitted', 0)}")
    print(f"  Blocked:         {last_crawl.get('applications_blocked', 0)}")
    print()

    high_score = report.get("new_high_score_events", [])
    if high_score:
        print("  HIGH-SCORE EVENTS:")
        for ev in high_score:
            print(f"    {ev.get('name', '?')}: {ev.get('score', 0)}/100")
            print(f"    Travel: {ev.get('travel', '?')}")
            print()

    top_apps = report.get("top_applications", [])
    if top_apps:
        print("  TOP APPLICATIONS:")
        for app in top_apps:
            print(f"    {app.get('event_name', '?')}: Score {app.get('score', 0)}, "
                  f"Status: {app.get('status', '?')}")
            print()

    errors = report.get("errors", [])
    if errors:
        print("  ERRORS:")
        for err in errors:
            print(f"    - {err}")
        print()

    print("=" * 60)


def print_profile() -> None:
    """Print the current profile."""
    print()
    print("=" * 60)
    print("  PHILIP'S PROFILE")
    print("=" * 60)
    print()
    print(f"  Name:       {profile.name}")
    print(f"  Location:   {profile.city}, {profile.country}")
    print(f"  Age:        {profile.age}")
    print(f"  Education:  {profile.education}")
    print()
    print("  Interests:")
    for interest in profile.interests:
        print(f"    - {interest}")
    print()
    print("  Hackathon Experience:")
    for exp in profile.hackathon_experience:
        print(f"    - {exp}")
    print()
    print("  Products:")
    for prod in profile.products:
        print(f"    - {prod.get('name')}: {prod.get('description', '')}")
    print()
    print("  Contact:")
    for key, val in profile.contact.items():
        if val:
            print(f"    {key}: {val}")
        else:
            print(f"    {key}: [not configured]")
    print()
    print("  Files:")
    for key, val in profile.data.get("files", {}).items():
        print(f"    {key}: {val}")
    print()
    print("=" * 60)


def print_settings() -> None:
    """Print current settings."""
    print()
    print("=" * 60)
    print("  SETTINGS")
    print("=" * 60)
    print()
    for field_name in settings.__dataclass_fields__:
        if field_name.startswith("_"):
            continue
        value = getattr(settings, field_name, None)
        print(f"  {field_name}: {value}")
    print()
    print("=" * 60)
