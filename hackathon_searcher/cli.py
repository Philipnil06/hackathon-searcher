"""
CLI entry point for the Hackathon Searcher.

Usage:
    python -m hackathon_searcher.cli run        # Run full daily pipeline
    python -m hackathon_searcher.cli dashboard  # Show dashboard
    python -m hackathon_searcher.cli events     # List events
    python -m hackathon_searcher.cli show ID    # Show event details
    python -m hackathon_searcher.cli profile    # Show profile
    python -m hackathon_searcher.cli settings   # Show settings
    python -m hackathon_searcher.cli report     # Show last report
    python -m hackathon_searcher.cli init       # Initialize database
"""

import sys

from hackathon_searcher.database import init_db
from hackathon_searcher.dashboard import (
    print_overview,
    print_events_table,
    print_event_detail,
    print_daily_report,
    print_profile,
    print_settings,
)
from hackathon_searcher.agent import run_daily_pipeline


def main() -> None:
    if len(sys.argv) < 2:
        print("Hackathon Searcher — Autonomous Hackathon Search & Application Agent")
        print()
        print("Commands:")
        print("  run         Run full daily pipeline")
        print("  dashboard   Show dashboard overview")
        print("  events      List all events")
        print("  show ID     Show event details")
        print("  profile     Show current profile")
        print("  settings    Show current settings")
        print("  report      Show last daily report")
        print("  init        Initialize database")
        print()
        return

    cmd = sys.argv[1].lower()

    if cmd == "init":
        init_db()
        print("Database initialized.")

    elif cmd == "run":
        print("Starting daily pipeline...")
        report = run_daily_pipeline()

        print()
        print("=" * 60)
        print("  DAILY HACKATHON SEARCH — Complete")
        print("=" * 60)
        print(f"  Scanned:      {report.events_scanned} events")
        print(f"  New:          {report.events_new}")
        print(f"  Updated:      {report.events_updated}")
        print(f"  Submitted:    {report.applications_submitted}")
        print(f"  Blocked:      {report.applications_blocked}")
        print()

        if report.top_applications:
            print("  TOP APPLICATIONS:")
            for app in report.top_applications:
                print(f"    {app.get('event_name', '?')}: Score {app.get('score', 0)}, "
                      f"Status: {app.get('status', '?')}")
            print()

        if report.new_high_score_events:
            print("  HIGH-SCORE EVENTS:")
            for ev in report.new_high_score_events:
                print(f"    {ev.get('name', '?')}: {ev.get('score', 0)}/100")
            print()

        if report.errors:
            print(f"  Errors: {len(report.errors)}")
            for err in report.errors:
                print(f"    - {err}")
            print()

        print("=" * 60)

    elif cmd == "dashboard":
        print_overview()

    elif cmd == "events":
        status_filter = sys.argv[2] if len(sys.argv) > 2 else None
        print_events_table(status_filter)

    elif cmd == "show":
        if len(sys.argv) < 3:
            print("Usage: python -m hackathon_searcher.cli show EVENT_ID")
            return
        event_id = sys.argv[2]
        print_event_detail(event_id)

    elif cmd == "profile":
        print_profile()

    elif cmd == "settings":
        print_settings()

    elif cmd == "report":
        print_daily_report()

    else:
        print(f"Unknown command: {cmd}")
        print("Available: run, dashboard, events, show, profile, settings, report, init")


if __name__ == "__main__":
    main()
