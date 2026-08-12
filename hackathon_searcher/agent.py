"""
Agent orchestrator — multi-applicant edition.

Runs the full daily pipeline for all applicants:
1. Discover events
2. Research events (external websites, LLM analysis)
3. Score events + per-applicant fit
4. Generate per-applicant applications
5. Submit (or dry-run simulate)
6. Produce daily report

Designed to be idempotent and failure-isolated per event per applicant.
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional

from hackathon_searcher.database import (
    init_db, get_event_by_id, get_events_needing_research,
    get_events_ready_to_apply, has_application, get_application,
    insert_application, update_application, update_event,
    log_audit, save_crawl_state, get_all_events, get_all_applications,
    get_applications_for_event, create_application_group,
)
from hackathon_searcher.event_research import research_event
from hackathon_searcher.forms import (
    extract_form_fields, generate_answer_for_field,
    create_submission_snapshot, validate_application,
)
from hackathon_searcher.llm import (
    analyze_event_page, estimate_travel_support_probability,
    review_application_quality, complete_structured, complete,
)
from hackathon_searcher.models import FormField, FormSnapshot, DailyReport
from hackathon_searcher.profile import profile_manager, ApplicantProfile
from hackathon_searcher.scoring import score_event, score_applicant_fit
from hackathon_searcher.scraper import discover_events, process_discovered_events
from hackathon_searcher.settings import settings


def run_daily_pipeline() -> DailyReport:
    """Run the full daily pipeline for all applicants."""
    print("=" * 60)
    print("DAILY HACKATHON SEARCH — Multi-Applicant Pipeline")
    print(f"Applicants: {', '.join(profile_manager.applicant_ids)}")
    print(f"Dry run: {settings.DRY_RUN}")
    print("=" * 60)

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
                update_event(event["event_id"], {"status": "RESEARCHING"})
                findings = research_event(event["event_id"])

                # LLM-enhanced research if we have page content
                if findings.get("all_text"):
                    try:
                        llm_analysis = analyze_event_page(
                            findings["all_text"],
                            event.get("event_name", "")
                        )
                        if llm_analysis:
                            _merge_llm_findings(event["event_id"], llm_analysis)
                    except Exception as e:
                        print(f"  LLM analysis failed for {event.get('event_name')}: {e}")

                # Estimate travel support probability if not confirmed
                ts = findings.get("travel_support", "") or event.get("travel_support", "")
                if ts in ("NO_TRAVEL_INFORMATION", "POSSIBLE_TRAVEL_SUPPORT", "UNKNOWN", ""):
                    try:
                        prob_result = estimate_travel_support_probability(
                            event, [findings.get("all_text", "")]
                        )
                        if prob_result:
                            update_event(event["event_id"], {
                                "travel_support_probability": prob_result.get("probability", 0.0),
                            })
                    except Exception:
                        pass

                log_audit(event["event_id"], "RESEARCHED", f"{len(findings)} fields updated")
            except Exception as e:
                errors.append(f"Research failed for {event.get('event_id')}: {e}")
                print(f"  ERROR: {e}")
    except Exception as e:
        errors.append(f"Research phase failed: {e}")

    # === Step 3: Score ===
    print("\n[3/5] SCORING EVENTS & APPLICANTS...")
    high_score_events = []
    try:
        events_to_score = get_events_needing_research()
        print(f"  Scoring {len(events_to_score)} events...")
        for event in events_to_score:
            try:
                event_result = score_event(event["event_id"])
                for applicant_id in profile_manager.applicant_ids:
                    fit = score_applicant_fit(event["event_id"], applicant_id)
                    if fit.get("should_apply"):
                        # Create application record
                        app_id = f"app_{event['event_id'][:12]}_{applicant_id}"
                        if not has_application(event["event_id"], applicant_id):
                            insert_application({
                                "application_id": app_id,
                                "event_id": event["event_id"],
                                "applicant_id": applicant_id,
                                "applicant_name": profile_manager.get(applicant_id).name,
                                "application_url": event.get("application_url", ""),
                                "date_started": datetime.now(timezone.utc).isoformat(),
                                "status": "QUALIFIED",
                                "event_score": event_result.get("event_score", 0),
                                "applicant_fit_score": fit.get("fit_score", 0),
                                "eligibility_status": "ELIGIBLE" if fit.get("eligible") else "INELIGIBLE",
                                "eligibility_reasoning": fit.get("eligibility_reasoning", ""),
                                "travel_eligible": 1 if fit.get("travel_eligible") else 0,
                                "travel_support_requested": 1,
                                "score_reasoning": fit.get("apply_reason", ""),
                                "travel_support_status": event.get("travel_support", ""),
                            })
                            log_audit(event["event_id"], "APPLICANT_QUALIFIED",
                                      f"{applicant_id}: fit={fit.get('fit_score')}", applicant_id=applicant_id)

                if event_result.get("event_score", 0) >= settings.NOTIFY_HIGH_SCORE_THRESHOLD:
                    high_score_events.append({
                        "name": event.get("event_name"),
                        "score": event_result.get("event_score"),
                        "travel": event.get("travel_support", "Unknown"),
                    })
            except Exception as e:
                errors.append(f"Scoring failed for {event.get('event_id')}: {e}")

        report.new_high_score_events = high_score_events
    except Exception as e:
        errors.append(f"Scoring phase failed: {e}")

    # === Step 4: Apply ===
    print("\n[4/5] PROCESSING APPLICATIONS...")
    submitted = 0
    blocked = 0
    try:
        for applicant_id in profile_manager.applicant_ids:
            apps = get_all_applications(applicant_id)
            for app in apps:
                if app.get("status") in ("QUALIFIED", "READY_TO_APPLY"):
                    try:
                        result = process_application_for_applicant(
                            app["event_id"], applicant_id
                        )
                        if result.get("status") == "APPLIED" or result.get("status") == "DRY_RUN_COMPLETE":
                            submitted += 1
                        elif result.get("status", "").startswith("BLOCKED"):
                            blocked += 1
                    except Exception as e:
                        errors.append(f"Application failed for {applicant_id} @ {app.get('event_id')}: {e}")
    except Exception as e:
        errors.append(f"Application phase failed: {e}")

    report.applications_submitted = submitted
    report.applications_blocked = blocked
    print(f"  Submitted: {submitted}, Blocked: {blocked}")

    # === Step 5: Report ===
    print("\n[5/5] BUILDING REPORT...")
    try:
        apps = get_all_applications()
        report.top_applications = [
            {
                "event_name": a.get("event_name"),
                "applicant": a.get("applicant_name"),
                "score": a.get("applicant_fit_score"),
                "status": a.get("status"),
                "travel": a.get("travel_support_status"),
            }
            for a in apps[:10]
        ]
    except Exception as e:
        errors.append(f"Report failed: {e}")

    report.errors = errors

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


def process_application_for_applicant(event_id: str, applicant_id: str) -> dict:
    """
    Process an application for a specific event + applicant.

    Steps:
    1. Check duplicates
    2. Get applicant profile
    3. Find/fetch application form
    4. Extract form fields
    5. Generate answers per field
    6. Validate
    7. Create snapshot
    8. Submit (or dry-run)
    """
    event = get_event_by_id(event_id)
    if not event:
        return {"status": "ERROR", "error": "Event not found"}

    profile = profile_manager.get(applicant_id)
    if not profile:
        return {"status": "ERROR", "error": f"Applicant {applicant_id} not found"}

    event_name = event.get("event_name", "")

    # Duplicate check
    if has_application(event_id, applicant_id):
        existing = get_application(event_id, applicant_id)
        if existing and existing.get("status") not in ("QUALIFIED", "READY_TO_APPLY", "SKIPPED", "INELIGIBLE"):
            log_audit(event_id, "DUPLICATE_SKIP", f"{applicant_id}: already applied", applicant_id=applicant_id)
            return {"status": "DUPLICATE_SKIPPED"}

    update_application(event_id, applicant_id, {"status": "READY_TO_APPLY"})
    log_audit(event_id, "APPLICATION_START", f"{applicant_id}: starting", applicant_id=applicant_id)

    # Determine application URL
    application_url = event.get("application_url", "") or event.get("event_url", "")
    if not application_url:
        try:
            from hackathon_searcher.browser import discover_application_form
            discovered = discover_application_form(event.get("event_url", ""), applicant_id)
            if discovered:
                application_url = discovered
                update_event(event_id, {"application_url": discovered})
        except ImportError:
            pass

    if not application_url:
        log_audit(event_id, "NO_APPLICATION_URL", "", applicant_id=applicant_id)
        update_application(event_id, applicant_id, {"status": "BLOCKED_LOGIN"})
        return {"status": "BLOCKED_LOGIN"}

    # Extract form fields
    fields: list[FormField] = []
    try:
        from hackathon_searcher.event_research import fetch_page
        html = fetch_page(application_url)
        if html:
            fields = extract_form_fields(html, application_url)
    except Exception as e:
        print(f"[agent] Form fetch failed: {e}")

    if not fields:
        try:
            from hackathon_searcher.browser import BrowserSession
            with BrowserSession() as browser:
                if browser.navigate(application_url):
                    html = browser.get_page_html()
                    fields = extract_form_fields(html, application_url)
        except Exception as e:
            print(f"[agent] Browser form extraction failed: {e}")

    # Build event context
    themes_raw = event.get("themes", "[]")
    try:
        themes = json.loads(themes_raw) if isinstance(themes_raw, str) else themes_raw
    except (json.JSONDecodeError, TypeError):
        themes = []

    event_context = {
        "event_name": event_name,
        "themes": themes,
        "travel_support": event.get("travel_support", ""),
        "organizer": event.get("organizer", ""),
        "description": event.get("description", ""),
        "city": event.get("city", ""),
        "country": event.get("country", ""),
    }

    # Generate answers
    unknown_required = []
    answers = []
    for field in fields:
        answer = generate_answer_for_field(field, profile, event_context, use_llm=True)
        field.answer = answer
        answers.append({"label": field.label, "answer": answer, "source": field.answer_source})
        if answer == "UNKNOWN_REQUIRED_FIELD":
            unknown_required.append(field)

    # Handle unknown required fields
    if unknown_required:
        blocked_reason = f"Unknown fields: {[f.label for f in unknown_required]}"
        log_audit(event_id, "BLOCKED_UNKNOWN_FIELD", blocked_reason, applicant_id=applicant_id)
        update_application(event_id, applicant_id, {
            "status": "BLOCKED_UNKNOWN_FIELD",
            "questions": json.dumps([f.model_dump() for f in fields]),
            "answers": json.dumps(answers),
            "notes": blocked_reason,
        })
        return {"status": "BLOCKED_UNKNOWN_FIELD", "error": blocked_reason}

    # Validate
    issues = validate_application(fields)
    if issues:
        print(f"[agent] Issues for {applicant_id} @ {event_name}: {issues}")

    # LLM quality review
    fit_score = 0
    existing_app = get_application(event_id, applicant_id)
    if existing_app:
        fit_score = existing_app.get("applicant_fit_score", 0)

    try:
        quality = review_application_quality(answers, profile.data, event_context)
        if not quality.get("passes", True):
            print(f"[agent] Quality issues for {applicant_id}: {quality.get('issues')}")
    except Exception as e:
        print(f"[agent] Quality review failed: {e}")

    # Create snapshot
    snapshot = create_submission_snapshot(
        event_id=event_id, applicant_id=applicant_id,
        event_name=event_name, application_url=application_url,
        fields=fields, event_score=event.get("event_score", 0),
        fit_score=fit_score,
        reason=event.get("score_reasoning", ""),
        travel_support_status=event.get("travel_support", ""),
    )

    # Submit via browser
    submit_result = {"status": "DRY_RUN_COMPLETE", "confirmation_text": "", "error": ""}

    if not settings.DRY_RUN and settings.AUTO_APPLY:
        try:
            from hackathon_searcher.browser import fill_application_form
            submit_result = fill_application_form(
                application_url=application_url,
                fields=fields,
                profile=profile,
                dry_run=False,
            )
        except ImportError:
            submit_result = {"status": "BLOCKED_LOGIN", "error": "Playwright not available"}
        except Exception as e:
            submit_result = {"status": "BLOCKED_LOGIN", "error": str(e)}
    else:
        # Dry run: simulate browser filling
        try:
            from hackathon_searcher.browser import fill_application_form
            submit_result = fill_application_form(
                application_url=application_url,
                fields=fields,
                profile=profile,
                dry_run=True,
            )
        except ImportError:
            submit_result["status"] = "DRY_RUN_COMPLETE"

    # Update application record
    now = datetime.now(timezone.utc).isoformat()
    update_application(event_id, applicant_id, {
        "application_url": application_url,
        "questions": json.dumps([f.model_dump() for f in fields]),
        "answers": json.dumps(answers),
        "date_submitted": now if submit_result["status"] == "APPLIED" else "",
        "application_confirmation": submit_result.get("confirmation_text", ""),
        "submission_snapshot": snapshot.model_dump_json(),
        "status": submit_result["status"],
        "notes": submit_result.get("error", ""),
    })

    log_audit(event_id, "APPLICATION_COMPLETE",
              f"{applicant_id}: {submit_result['status']}", applicant_id=applicant_id)

    # Create application group if both applicants are applying
    _maybe_create_group(event_id)

    return submit_result


def _merge_llm_findings(event_id: str, llm_analysis: dict) -> None:
    """Merge LLM analysis results into the event record."""
    updates = {}
    if llm_analysis.get("travel_support_status"):
        updates["travel_support"] = llm_analysis["travel_support_status"]
    if llm_analysis.get("travel_support_details"):
        updates["travel_support_details"] = json.dumps(llm_analysis["travel_support_details"])
    if llm_analysis.get("travel_support_amount"):
        updates["travel_support_amount"] = llm_analysis["travel_support_amount"]
    if llm_analysis.get("sponsors"):
        updates["sponsors"] = json.dumps(llm_analysis["sponsors"])
    if llm_analysis.get("themes"):
        updates["themes"] = json.dumps(llm_analysis["themes"])
    if llm_analysis.get("eligibility_requirements"):
        updates["eligibility_rules_raw"] = llm_analysis["eligibility_requirements"]
    if updates:
        try:
            update_event(event_id, updates)
        except Exception as e:
            print(f"[agent] Failed to merge LLM findings: {e}")


def _maybe_create_group(event_id: str) -> None:
    """Create an application group if both applicants have applications for this event."""
    apps = get_applications_for_event(event_id)
    applicant_ids = [a.get("applicant_id") for a in apps if a.get("applicant_id")]

    if len(applicant_ids) >= 2:
        group_id = f"grp_{event_id[:16]}_{'_'.join(sorted(applicant_ids)[:2])}"
        create_application_group(group_id, event_id, applicant_ids)
        for app in apps:
            update_application(app["event_id"], app["applicant_id"], {"application_group_id": group_id})
