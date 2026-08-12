"""
CLI entry point for the Hackathon Searcher — multi-applicant edition.

Usage:
    python -m hackathon_searcher.cli daily      # Efficient daily pipeline (for Task Scheduler)
    python -m hackathon_searcher.cli preflight  # Real-event preparation, no Submit
    python -m hackathon_searcher.cli run        # Run full research pipeline
    python -m hackathon_searcher.cli dashboard  # Show dashboard
    python -m hackathon_searcher.cli events     # List events
    python -m hackathon_searcher.cli show ID    # Show event details
    python -m hackathon_searcher.cli profiles   # Show all profiles
    python -m hackathon_searcher.cli settings   # Show settings
    python -m hackathon_searcher.cli report     # Show last report
    python -m hackathon_searcher.cli init       # Initialize database
"""

import sys, json

from hackathon_searcher.database import init_db
from hackathon_searcher.dashboard import (
    print_overview, print_events_table, print_event_detail,
    print_daily_report, print_profile_summary, print_settings,
)
from hackathon_searcher.agent import run_daily_pipeline
from hackathon_searcher.daily import run_daily_pipeline as run_efficient_daily, run_current_data_preflight
from hackathon_searcher.live_submit import run_targeted_application_discovery, prepare_ready_to_apply, select_targeted_event_ids, validate_luma_form_fill
from hackathon_searcher.database import get_application, get_event_by_id
from hackathon_searcher.profile import profile_manager
from hackathon_searcher.auth import run_auth_setup, print_auth_status
from hackathon_searcher.human_assist import (
    create_human_assist_package, mark_manually_submitted, open_human_assist,
    open_team_human_assist, pending_human_assist_events, provider_capability_table,
)
from hackathon_searcher.onboarding import improve_profile, run_setup, validate_profile
from hackathon_searcher.team import add_member, load_team, remove_member
from hackathon_searcher.dedicated_browser import browser_setup, browser_verify
from hackathon_searcher.llm import llm_configuration_status
from hackathon_searcher.preflight import local_preflight_checks
from hackathon_searcher.scheduler import remove_scheduler, scheduler_status, setup_scheduler
from hackathon_searcher.settings import settings


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1].lower() in {"--help", "-h", "help"}:
        print("Hackathon Searcher — Multi-Applicant Edition")
        print()
        print("Commands:")
        print("  setup       Create local profiles, a team, and LLM settings")
        print("  preflight   Check local readiness; never submits")
        print("  daily       Discover and prepare opportunities (use --dry-run to force safe mode)")
        print("  profile     Show, validate, or improve an applicant profile")
        print("  team        Show or change the local team")
        print("  llm         Check LLM configuration without an API call")
        print("  browser     Set up or verify the dedicated Chrome profile")
        print("  schedule    Set up, inspect, or remove the Windows daily task")
        print("  discover    Deeply inspect the ten priority application flows; no Submit")
        print("  validate-luma  Fill safe Luma fields in DRY_RUN; never Submit")
        print("  human-assist [open|mark-submitted ID]  Prepared Luma manual-submission packages")
        print("  auth-setup  Manual authentication for genuinely login-gated platforms")
        print("  auth-status Validate saved sessions for genuinely login-gated platforms")
        print("  run         Run full research pipeline")
        print("  dashboard   Show dashboard overview")
        print("  events      List all events")
        print("  show ID     Show event details (with per-applicant answers)")
        print("  profiles    Show all applicant profiles")
        print("  settings    Show current settings")
        print("  report      Show last daily report")
        print("  init        Initialize database")
        print()
        return

    cmd = sys.argv[1].lower()

    if cmd == "init":
        init_db()
        print("Database initialized (multi-applicant schema).")

    elif cmd == "setup":
        try:
            team = run_setup()
            print(f"Setup complete: {team.team_id} with {len(team.members)} applicant(s).")
            print("\nNext:\n\n  python -m hackathon_searcher.cli preflight\n  python -m hackathon_searcher.cli daily --dry-run\n\nOptional Luma browser setup:\n\n  python -m hackathon_searcher.cli browser setup")
        except ValueError as exc:
            print(f"Setup needs attention: {exc}")

    elif cmd == "llm":
        action = sys.argv[2].lower() if len(sys.argv) > 2 else "verify"
        if action != "verify":
            print("Usage: llm verify")
            return
        result = llm_configuration_status()
        print(f"Provider: {result['provider']}")
        print(f"Model: {result['model'] or 'missing'}")
        print(f"Status: {'READY' if result['ready'] else 'NEEDS SETUP'}")
        print(result["message"])

    elif cmd == "schedule":
        action = sys.argv[2].lower() if len(sys.argv) > 2 else "status"
        try:
            if action == "setup":
                result = setup_scheduler()
                print("Windows daily schedule is ready. It runs at 09:00 local time in the background.")
                print(f"Task: {result.get('task_name', 'Hackathon Searcher Daily')}")
                print(f"Executable: {result.get('executable', '')}")
                print(f"Working directory: {result.get('working_directory', '')}")
                print(f"DRY_RUN currently: {settings.DRY_RUN}")
            elif action == "status":
                result = scheduler_status()
                if not result.get("supported"):
                    print(result["message"])
                elif not result.get("exists"):
                    print("No Hackathon Searcher daily task is installed. Run `schedule setup` to create it.")
                else:
                    print(f"Task: {result['task_name']} ({result['state']})")
                    print(f"Schedule: daily 09:00 local time; next run {result['next_run_time']}")
                    print(f"Missed start: {'enabled' if result['start_when_available'] else 'disabled'}")
                    print(f"Multiple instances: {result['multiple_instances']}")
                    print(f"Executable: {result['executable']}")
                    print(f"Working directory: {result['working_directory']}")
                    print(f"Last result: {result['last_run_result']}")
            elif action == "remove":
                removed = remove_scheduler(confirmed="--yes" in sys.argv)
                print("Scheduled task removed." if removed else "No Hackathon Searcher daily task was installed.")
            else:
                print("Usage: schedule setup | schedule status | schedule remove --yes")
        except RuntimeError as exc:
            print(f"Scheduling needs attention: {exc}")

    elif cmd == "browser":
        action = sys.argv[2].lower() if len(sys.argv) > 2 else "verify"
        try:
            if action == "setup":
                result = browser_setup()
                print("Hackathon Searcher browser setup\n")
                print("1. Enable Developer mode\n2. Click 'Load unpacked'\n3. Select this folder:\n")
                print(f"{result['extension']}\n")
                print("After installation, return here and run:\n\n  python -m hackathon_searcher.cli browser verify")
            elif action == "verify":
                result = browser_verify()
                print("Hackathon Searcher Browser\n")
                print(f"Chrome: {'OK' if result['chrome'] else 'not found'}")
                print(f"Dedicated profile: {'OK' if result['profile_exists'] and result['profile_safe'] else 'not ready'}")
                print(f"Local bridge: {'connected' if result['bridge'] else 'unavailable'}")
                if result['extension']:
                    print(f"Extension: connected ({result['extension'].get('extension_version', 'version unknown')})")
                else:
                    print("Extension: not detected\n\nRun:\n  python -m hackathon_searcher.cli browser setup")
                if result['luma_session'] == 'present':
                    print("\nLUMA_SESSION_PRESENT\n\nThis dedicated Hackathon Searcher browser is currently logged into Luma.\nLog out of Luma before continuing so applicant identities are not mixed.")
                else:
                    print(f"Luma session: {result['luma_session']}")
                if result['chrome'] and result['profile_safe'] and result['bridge']:
                    print("\nBrowser setup complete.")
            elif action == "open":
                result = browser_verify()
                if not result['chrome']:
                    raise RuntimeError("Google Chrome was not found. Install Chrome, then run browser setup.")
                from hackathon_searcher.dedicated_browser import launch_dedicated_chrome
                launch_dedicated_chrome(["about:blank"])
                print("Opened the dedicated Hackathon Searcher Chrome profile.")
            else:
                print("Usage: browser setup | browser verify | browser open")
        except RuntimeError as exc:
            print(f"Browser setup failed: {exc}")

    elif cmd == "profile":
        action = sys.argv[2].lower() if len(sys.argv) > 2 else "show"
        if action == "show":
            profile_manager.reload()
            for applicant_id in profile_manager.applicant_ids:
                profile = profile_manager.get(applicant_id)
                print(f"{applicant_id}: {profile.full_name} | {profile.email} | {profile.city}, {profile.country}")
        elif action == "validate" and len(sys.argv) > 3:
            issues = validate_profile(sys.argv[3])
            print("VALID" if not issues else "INVALID: " + "; ".join(issues))
        elif action == "improve" and len(sys.argv) > 3:
            improve_profile(sys.argv[3])
            print("Profile updated locally.")
        else:
            print("Usage: profile show | profile validate APPLICANT_ID | profile improve APPLICANT_ID")

    elif cmd == "team":
        action = sys.argv[2].lower() if len(sys.argv) > 2 else "show"
        try:
            if action == "show":
                team = load_team()
            elif action == "add" and len(sys.argv) > 3:
                team = add_member(sys.argv[3])
            elif action == "remove" and len(sys.argv) > 3:
                team = remove_member(sys.argv[3])
            else:
                print("Usage: team show | team add APPLICANT_ID | team remove APPLICANT_ID")
                return
            print(f"Team {team.team_id}: {', '.join(team.members)} | minimum score {team.minimum_team_score:g}")
        except ValueError as exc:
            print(f"Team update failed: {exc}")

    elif cmd == "daily":
        dry_run = "--dry-run" in sys.argv
        if dry_run:
            print("Running daily pipeline in DRY RUN mode...")
        else:
            print("Running daily pipeline...")
        state = run_efficient_daily(dry_run_override=True if dry_run else None)
        if state.get("status") == "SKIPPED_ALREADY_RUNNING":
            print("Exiting: another daily run is already active.")

    elif cmd == "preflight":
        print("LOCAL READINESS CHECK — no submission and no LLM request")
        checks = local_preflight_checks()
        for profile in checks["profiles"]:
            status = "OK" if profile["valid"] else "NEEDS ATTENTION"
            print(f"Profile {profile['applicant_id']}: {status}")
            for issue in profile["issues"]:
                print(f"  - {issue}")
        team = checks.get("team")
        print(f"Team: {', '.join(team['members']) if team else 'not configured'}")
        print(f"Database: {'OK' if checks.get('database', {}).get('ready') else 'NEEDS ATTENTION'}")
        print(f"LLM: {checks['llm']['message']}")
        browser = checks["browser"]
        print(f"Browser (optional): {'Chrome found' if browser['chrome_found'] else 'Chrome not found'}; extension source {'OK' if browser['extension_source'] else 'missing'}")
        scheduler = checks["scheduler"]
        print(f"Scheduler (optional): {'installed' if scheduler.get('exists') else 'not installed'}")
        for warning in checks["warnings"]:
            print(f"Optional: {warning}")
        if checks["issues"]:
            print("\nPreflight stopped before application checks. Fix the items above, then rerun preflight.")
            return
        print("\nRunning submission-disabled preflight using current verified data...")
        state = run_current_data_preflight()
        if state.get("status") == "SKIPPED_ALREADY_RUNNING":
            print("Exiting: another daily run is already active.")

    elif cmd == "discover":
        print("Running targeted application discovery; no Submit action is enabled...")
        init_db()
        reports = run_targeted_application_discovery()
        targeted_ids = set(select_targeted_event_ids())
        print()
        print("APPLICATION DISCOVERY REPORT")
        for report in reports:
            print(f"\nEVENT: {report['event_name']}")
            print(f"Official URL: {report['official_url']}")
            print(f"Event score: {report['event_score']:.1f}")
            print(f"Deadline: {report['application_deadline'] or 'unknown'}")
            print(f"Travel support: {report['travel_support']}")
            for applicant_id, flow in report["applicant_flows"].items():
                app = get_application(report["event_id"], applicant_id) or {}
                eligible = app.get("eligibility_status", "UNCERTAIN")
                state = flow.get("status", "APPLICATION_DISCOVERY_FAILED")
                can_apply = state == "APPLICATION_FORM_FOUND" and eligible == "ELIGIBLE"
                print(f"  Applicant: {app.get('applicant_name', applicant_id)}")
                print(f"    Current application state: {state}")
                print(f"    Application URL: {flow.get('url', '') or 'unknown'}")
                print(f"    Discovery path: {json.dumps(flow.get('application_discovery_path', []), ensure_ascii=False)}")
                print(f"    Form provider: {flow.get('provider', '') or 'unknown'}")
                print(f"    Authentication required: {'yes' if state == 'AUTH_REQUIRED' else 'no'}")
                print(f"    Application open date: {flow.get('application_open_date', '') or 'unknown'}")
                print(f"    Can {app.get('applicant_name', applicant_id)} apply now: {'yes' if can_apply else 'no'} ({eligible})")
                print(f"    Blocker: {flow.get('reason', '') or 'none'}")
                print(f"    Next action: {'manual authentication setup' if state == 'AUTH_REQUIRED' else ('complete stored form checks' if can_apply else 'monitor/research')}")
        ready = prepare_ready_to_apply(preflight=True, event_ids=targeted_ids)
        print("\nREADY_TO_APPLY")
        if ready:
            for item in ready:
                print(f"  {item['applicant_name']} | {item['event_name']} | {item['application_url']} | {item['form_provider']}")
                app = get_application(item['event_id'], item['applicant_id']) or {}
                print(f"    Questions: {json.dumps(app.get('questions', []), ensure_ascii=False)}")
                print(f"    Answers: {json.dumps(app.get('answers', []), ensure_ascii=False)}")
                print(f"    Travel support: {item['travel_support']} | Eligibility: {item['eligibility']}")
        else:
            print("  none")

        print("\nAUTHENTICATION SETUP REQUIRED")
        auth_found = False
        for report in reports:
            for applicant_id, flow in report["applicant_flows"].items():
                if flow.get("status") == "AUTH_REQUIRED":
                    auth_found = True
                    app = get_application(report["event_id"], applicant_id) or {}
                    profile = profile_manager.get(applicant_id)
                    fallback_account = profile.email if profile else "configured account"
                    print(f"  {app.get('applicant_name', applicant_id)} | {report['event_name']} | platform={flow.get('auth_platform', '')} | login={flow.get('auth_login_url', '')} | account={flow.get('auth_account', '') or fallback_account}")
        if not auth_found:
            print("  none")
        print("\nMONITORED FOR OPENING")
        monitored = [report for report in reports if any(state == "APPLICATION_NOT_OPEN_YET" for state in report["states"])]
        if monitored:
            for report in monitored:
                print(f"  {report['event_name']} | next check: 24h | opening: unknown or stored on event")
        else:
            print("  none")

    elif cmd == "validate-luma":
        print("Running submission-disabled Luma form-fill validation...")
        init_db()
        reports = validate_luma_form_fill()
        for report in reports:
            print(f"\nEVENT: {report['event']} | APPLICANT: {report.get('applicant', '')}")
            print(f"  STATUS: {report['status']} | ELIGIBILITY: {report.get('eligibility', '')}")
            print(f"  URL: {report.get('application_url', '')}")
            print(f"  QUESTIONS_DETECTED: {len(report.get('questions', []))}")
            print(f"  FACT_CHECK_PASSED: {report.get('fact_check_passed', False) if report.get('answers') else 'N/A'}")
            print(f"  CROSS_PROFILE_CHECK_PASSED: {report.get('cross_profile_check_passed', False) if report.get('answers') else 'N/A'}")
            print(f"  FIELDS_FILLED: {report.get('fields_filled', 0)}")
            print(f"  CONSENT_POLICY_PASSED: {report.get('consent_policy_passed', 'N/A')}")
            for choice in report.get('consent_choices', []):
                print(f"    CONSENT [{choice['category']}]: {choice['choice']} — {choice['question']}")
            print("  FINAL_SUBMIT_NOT_CLICKED: True")
            answers = report.get('answers', [])
            for index, question in enumerate(report.get('questions', [])):
                answer = answers[index].get('answer', '') if index < len(answers) else 'N/A — eligibility gate prevented answer generation'
                print(f"    Q: {question.get('label', '')}\n    A: {answer}")
            if report.get('blocker'):
                print(f"  BLOCKER: {report['blocker']}")

    elif cmd == "human-assist":
        init_db()
        action = sys.argv[2].lower() if len(sys.argv) > 2 else "list"
        if action == "mark-submitted":
            if len(sys.argv) < 4:
                print("Usage: python -m hackathon_searcher.cli human-assist mark-submitted EVENT_ID")
                return
            mark_manually_submitted(sys.argv[3])
            print(f"Marked both applicants MANUALLY_SUBMITTED for {sys.argv[3]}.")
            return
        if action == "open":
            event_id = sys.argv[3] if len(sys.argv) > 3 else ""
            if len(sys.argv) > 4:
                applicant_id = sys.argv[4].lower()
                event, paths, selected_url = open_human_assist(event_id, applicant_id)
                print(f"Opened {applicant_id}: {selected_url}")
            else:
                event, paths, selected_urls = open_team_human_assist(event_id)
                print("Opened separately bound team tabs:")
                for applicant_id, url in selected_urls.items():
                    print(f"  {applicant_id}: {url}")
            print("Chrome Autofill: selected prepared application through 127.0.0.1:8765")
            for applicant_id, path in paths.items():
                print(f"{applicant_id}: {path}")
            return
        if action != "list":
            print("Usage: python -m hackathon_searcher.cli human-assist [open EVENT_ID [APPLICANT_ID]|mark-submitted EVENT_ID]")
            return
        events = pending_human_assist_events()
        print("HUMAN-ASSISTED LUMA APPLICATIONS")
        if not events:
            print("  none")
        for event in events:
            paths = create_human_assist_package(event["event_id"])
            print(f"  {event['event_id']} | {event.get('event_name', '')} | team={event.get('team_apply_score', 0):.2f} | {event.get('event_url', '')}")
            for applicant_id, path in paths.items():
                if applicant_id != "event":
                    print(f"    {applicant_id}: {path}")
            print(f"    Event: {paths.get('event', '')}")
        print("PROVIDER CAPABILITIES")
        for provider, capability in provider_capability_table():
            print(f"  {provider}: {capability}")

    elif cmd == "auth-setup":
        if len(sys.argv) < 3:
            print("Usage: python -m hackathon_searcher.cli auth-setup APPLICANT_ID")
            return
        applicant_id = sys.argv[2].lower()
        print("DRY_RUN=true — no application submission is available in auth setup.")
        run_auth_setup(applicant_id)

    elif cmd == "auth-status":
        print_auth_status()

    elif cmd == "run":
        print("Starting full research pipeline...")
        report = run_daily_pipeline()

        print()
        print("=" * 65)
        print("  DAILY HACKATHON SEARCH — Complete")
        print("=" * 65)
        print(f"  Scanned:      {report.events_scanned}")
        print(f"  New:          {report.events_new}")
        print(f"  Updated:      {report.events_updated}")
        print(f"  Submitted:    {report.applications_submitted}")
        print(f"  Blocked:      {report.applications_blocked}")
        print()

        if report.top_applications:
            print("  Applications:")
            for app in report.top_applications:
                print(f"    [{app.get('applicant', '?')}] {app.get('event_name', '?')}: "
                      f"Score {app.get('score', 0):.0f}, Status: {app.get('status', '?')}")
            print()

        if report.new_high_score_events:
            print("  High-Score Events:")
            for ev in report.new_high_score_events:
                print(f"    {ev.get('name', '?')}: {ev.get('score', 0)}/100")
            print()

        if report.errors:
            print(f"  Errors: {len(report.errors)}")
            for err in report.errors[:5]:
                print(f"    - {err}")
            print()

        print("=" * 65)

    elif cmd == "dashboard":
        print_overview()

    elif cmd == "events":
        status_filter = sys.argv[2] if len(sys.argv) > 2 else None
        print_events_table(status_filter)

    elif cmd == "show":
        if len(sys.argv) < 3:
            print("Usage: python -m hackathon_searcher.cli show EVENT_ID")
            return
        print_event_detail(sys.argv[2])

    elif cmd == "profiles":
        print_profile_summary()

    elif cmd == "settings":
        print_settings()

    elif cmd == "report":
        print_daily_report()

    else:
        print(f"Unknown command: {cmd}")
        print("Available: setup, profile, team, llm, browser, schedule, run, daily, preflight, discover, validate-luma, human-assist, auth-setup, auth-status, dashboard, events, show, profiles, settings, report, init")


if __name__ == "__main__":
    main()
