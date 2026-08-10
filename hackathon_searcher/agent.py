"""
Agent orchestrator — runs the full daily pipeline.

Coordinates discovery, research, scoring, and application submission.
Designed to be idempotent and failure-isolated per event.
"""

import json
from datetime import datetime, timezone
from typing import Optional

from hackathon_searcher.database import (
    init_db,
    get_event_by_id,
    get_events_needing_research,
    get_events_ready_to_apply,
    has_application,
    insert_application,
    update_application,
    update_event,
    log_audit,
    save_crawl_state,
    get_all_events,
    get_all_applications,
)
from hackathon_searcher.event_research import research_event
from hackathon_searcher.forms import (
    extract_form_fields,
    generate_answer,
    create_submission_snapshot,
    validate_application,
    detect_form_provider,
)
from hackathon_searcher.models import FormField, FormSnapshot, DailyReport
from hackathon_searcher.profile import profile
from hackathon_searcher.scoring import score_event
from hackathon_searcher.scraper import discover_events, process_discovered_events
from hackathon_searcher.settings import settings


def run_daily_pipeline() -> DailyReport:
    """
    Run the full daily pipeline:

    1. Discover new events from Hackathon Hub
    2. Research new/updated events
    3. Score all researched events
    4. Apply to qualified events
    5. Produce daily report
    """
    print("=" * 60)
    print("DAILY HACKATHON SEARCH — Starting pipeline")
    print(f"Dry run: {settings.DRY_RUN}")
    print(f"Auto apply: {settings.AUTO_APPLY}")
    print("=" * 60)

    # Ensure database is initialized
    init_db()

    report = DailyReport(timestamp=datetime.now(timezone.utc).isoformat())
    errors: list[str] = []

    # === Step 1: Discover ===
    print("\n[1/5] DISCOVERING EVENTS...")
    try:
        raw_events = discover_events()
        report.events_scanned = len(raw_events)
        print(f"  Scanned: {len(raw_events)} events")

        new_ids, updated_ids, skipped = process_discovered_events(raw_events)
        report.events_new = len(new_ids)
        report.events_updated = len(updated_ids)
        print(f"  New: {len(new_ids)}, Updated: {len(updated_ids)}, Skipped: {skipped}")
    except Exception as e:
        errors.append(f"Discovery failed: {e}")
        print(f"  ERROR: {e}")

    # === Step 2: Research ===
    print("\n[2/5] RESEARCHING EVENTS...")
    try:
        events_to_research = get_events_needing_research()
        print(f"  Researching {len(events_to_research)} events...")

        for event in events_to_research:
            try:
                # Mark as researching
                update_event(event["event_id"], {"status": "RESEARCHING"})

                findings = research_event(event["event_id"])

                # If no external URL, score anyway based on what we have
                if not event.get("event_url"):
                    log_audit(event["event_id"], "NO_EXTERNAL_URL", "No external event URL to research")

                log_audit(event["event_id"], "RESEARCHED", f"Findings: {len(findings)} fields updated")
            except Exception as e:
                errors.append(f"Research failed for {event.get('event_id')}: {e}")
                print(f"  ERROR researching {event.get('event_name')}: {e}")
    except Exception as e:
        errors.append(f"Research phase failed: {e}")
        print(f"  ERROR: {e}")

    # === Step 3: Score ===
    print("\n[3/5] SCORING EVENTS...")
    try:
        events_to_score = get_events_needing_research()
        print(f"  Scoring {len(events_to_score)} events...")
        high_score_events = []

        for event in events_to_score:
            try:
                result = score_event(event["event_id"])
                if result.get("total_score", 0) >= settings.NOTIFY_HIGH_SCORE_THRESHOLD:
                    high_score_events.append({
                        "name": event.get("event_name"),
                        "score": result.get("total_score"),
                        "reason": result.get("reasoning", ""),
                        "travel": event.get("travel_support", "Unknown"),
                    })
            except Exception as e:
                errors.append(f"Scoring failed for {event.get('event_id')}: {e}")
                print(f"  ERROR scoring {event.get('event_name')}: {e}")

        report.new_high_score_events = high_score_events
    except Exception as e:
        errors.append(f"Scoring phase failed: {e}")
        print(f"  ERROR: {e}")

    # === Step 4: Apply ===
    print("\n[4/5] PROCESSING APPLICATIONS...")
    applications_submitted = 0
    applications_blocked = 0

    try:
        qualified_events = get_events_ready_to_apply()
        print(f"  Qualified events: {len(qualified_events)}")

        for event in qualified_events:
            try:
                result = process_application(event)
                if result.get("status") == "APPLIED":
                    applications_submitted += 1
                elif result.get("status", "").startswith("BLOCKED"):
                    applications_blocked += 1
            except Exception as e:
                errors.append(f"Application failed for {event.get('event_id')}: {e}")
                print(f"  ERROR applying to {event.get('event_name')}: {e}")

        report.applications_submitted = applications_submitted
        report.applications_blocked = applications_blocked
        print(f"  Submitted: {applications_submitted}, Blocked: {applications_blocked}")
    except Exception as e:
        errors.append(f"Application phase failed: {e}")
        print(f"  ERROR: {e}")

    # === Step 5: Build report ===
    print("\n[5/5] BUILDING REPORT...")
    try:
        # Get top applications
        apps = get_all_applications()
        report.top_applications = [
            {
                "event_name": app.get("event_name"),
                "score": app.get("score"),
                "status": app.get("status"),
                "travel_support": app.get("travel_support_status"),
                "date_applied": app.get("date_applied"),
            }
            for app in apps[:5]
        ]
    except Exception as e:
        errors.append(f"Report building failed: {e}")

    report.errors = errors

    # Save crawl state
    try:
        save_crawl_state({
            "events_scanned": report.events_scanned,
            "events_new": report.events_new,
            "events_updated": report.events_updated,
            "applications_submitted": report.applications_submitted,
            "applications_blocked": report.applications_blocked,
            "report": json.dumps(report.model_dump(), default=str),
        })
    except Exception as e:
        errors.append(f"Failed to save crawl state: {e}")

    return report


def process_application(event: dict) -> dict:
    """
    Process an application for a qualified event.

    1. Check for duplicates
    2. Find the application form
    3. Extract and answer fields
    4. Create submission snapshot
    5. Submit (or simulate in dry run)
    6. Store results
    """
    event_id = event["event_id"]
    event_name = event.get("event_name", "")

    # Check for existing application (duplicate prevention)
    if has_application(event_id):
        log_audit(event_id, "DUPLICATE_SKIP", "Application already exists")
        return {"status": "DUPLICATE_SKIPPED"}

    # Update status
    update_event(event_id, {"status": "READY_TO_APPLY"})
    log_audit(event_id, "APPLICATION_START", f"Starting application for {event_name}")

    # Determine application URL
    application_url = event.get("application_url", "") or event.get("event_url", "")
    if not application_url:
        # Try to discover the application form from the event website
        try:
            from hackathon_searcher.browser import discover_application_form
            discovered = discover_application_form(event.get("event_url", ""))
            if discovered:
                application_url = discovered
                update_event(event_id, {"application_url": discovered})
        except ImportError:
            pass

    if not application_url:
        log_audit(event_id, "NO_APPLICATION_URL", "No application URL found")
        update_event(event_id, {"status": "BLOCKED_LOGIN"})
        return {"status": "BLOCKED_LOGIN", "error": "No application URL"}

    # Try to get the form HTML
    fields: list[FormField] = []
    try:
        from hackathon_searcher.event_research import fetch_page
        html = fetch_page(application_url)
        if html:
            fields = extract_form_fields(html, application_url)
    except Exception as e:
        print(f"[agent] Failed to fetch application form: {e}")

    if not fields:
        # Try with browser
        try:
            from hackathon_searcher.browser import BrowserSession
            with BrowserSession() as browser:
                if browser.navigate(application_url):
                    html = browser.get_page_html()
                    fields = extract_form_fields(html, application_url)
        except (ImportError, Exception) as e:
            print(f"[agent] Browser form extraction failed: {e}")

    if not fields:
        log_audit(event_id, "NO_FORM_FIELDS", "Could not extract form fields")
        # Still try to submit with just the URL
        fields = []

    # Generate answers for each field
    event_context = {
        "event_name": event_name,
        "themes": event.get("themes", []),
        "travel_support": event.get("travel_support", ""),
        "organizer": event.get("organizer", ""),
    }

    unknown_required = []
    for field in fields:
        answer = generate_answer(field, event_context)
        field.answer = answer
        if answer == "UNKNOWN_REQUIRED_FIELD":
            unknown_required.append(field)

    # If there are unknown required fields, block the application
    if unknown_required:
        blocked_reason = f"Unknown required fields: {[f.label for f in unknown_required]}"
        log_audit(event_id, "BLOCKED_UNKNOWN_FIELD", blocked_reason)
        update_event(event_id, {"status": "BLOCKED_UNKNOWN_FIELD"})

        # Store the application with what we have
        insert_application({
            "event_id": event_id,
            "event_name": event_name,
            "application_url": application_url,
            "questions": [f.model_dump() for f in fields],
            "answers": [{"label": f.label, "answer": f.answer} for f in fields],
            "status": "BLOCKED_UNKNOWN_FIELD",
            "score": event.get("score", 0),
            "score_reasoning": event.get("score_reasoning", ""),
            "travel_support_status": event.get("travel_support", ""),
            "notes": blocked_reason,
        })

        return {"status": "BLOCKED_UNKNOWN_FIELD", "error": blocked_reason}

    # Validate the application
    issues = validate_application(fields)
    if issues:
        print(f"[agent] Application issues for {event_name}: {issues}")

    # Create submission snapshot
    snapshot = create_submission_snapshot(
        event_id=event_id,
        event_name=event_name,
        application_url=application_url,
        fields=fields,
        score=event.get("score", 0),
        reason=event.get("score_reasoning", ""),
        travel_support_status=event.get("travel_support", ""),
    )

    # Try to submit via browser
    submit_result = {"status": "READY_TO_APPLY", "confirmation_text": "", "error": ""}

    if settings.AUTO_APPLY or not settings.DRY_RUN:
        try:
            from hackathon_searcher.browser import fill_application_form
            submit_result = fill_application_form(
                application_url=application_url,
                fields=fields,
                dry_run=settings.DRY_RUN,
            )
        except ImportError:
            submit_result["error"] = "Playwright not available"
            submit_result["status"] = "BLOCKED_LOGIN"
        except Exception as e:
            submit_result["error"] = str(e)
            submit_result["status"] = "BLOCKED_LOGIN"
    else:
        submit_result["status"] = "READY_TO_APPLY"
        submit_result["confirmation_text"] = "[Manual submit — auto apply disabled]"

    # Store the application
    now = datetime.now(timezone.utc).isoformat()
    insert_application({
        "event_id": event_id,
        "event_name": event_name,
        "application_url": application_url,
        "questions": [f.model_dump() for f in fields],
        "answers": [{"label": f.label, "answer": f.answer, "source": f.answer_source} for f in fields],
        "date_applied": now if submit_result["status"] == "APPLIED" else "",
        "application_confirmation": submit_result.get("confirmation_text", ""),
        "submission_snapshot": snapshot.model_dump(),
        "status": submit_result["status"],
        "score": event.get("score", 0),
        "score_reasoning": event.get("score_reasoning", ""),
        "travel_support_status": event.get("travel_support", ""),
        "notes": submit_result.get("error", ""),
    })

    # Update event status
    update_event(event_id, {"status": submit_result["status"]})

    log_audit(
        event_id,
        "APPLICATION_COMPLETE",
        f"Status: {submit_result['status']}, Confirmation: {submit_result.get('confirmation_text', '')[:200]}"
    )

    return submit_result


def run_single_event(event_id: str) -> dict:
    """
    Run the full pipeline for a single event. Useful for testing.
    """
    event = get_event_by_id(event_id)
    if not event:
        return {"error": f"Event {event_id} not found"}

    findings = research_event(event_id)
    score_result = score_event(event_id)
    app_result = process_application(event)

    return {
        "research": findings,
        "score": score_result,
        "application": app_result,
    }
