import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from hackathon_searcher import daily, human_assist, live_submit
from hackathon_searcher.team import TeamConfig


class LiveCycleIntegrationTests(unittest.TestCase):
    def test_current_data_preflight_uses_disabled_executor_without_research(self):
        candidate = {"event_id": "stored-event", "event_name": "Stored Event", "applicant_name": "Sam", "application_url": "https://example.test"}
        report = {"queue": [candidate], "attempted": [], "blocked": 0, "builder_count": 0, "teammate_count": 0}
        with patch.object(daily, "acquire_run_lock", return_value=True), \
             patch.object(daily, "release_run_lock"), \
             patch.object(daily, "is_daily_run_active", return_value=False), \
             patch.object(daily, "init_db"), \
             patch.object(daily, "start_daily_run", return_value="test-run"), \
             patch.object(daily, "get_all_applications", return_value=[{"event_id": "stored-event", "form_validation_status": "VALIDATED"}]), \
             patch.object(daily, "complete_daily_run"), \
             patch.object(daily, "_write_daily_report", return_value="test-report"), \
             patch("hackathon_searcher.live_submit.prepare_ready_to_apply", return_value=[candidate]), \
             patch("hackathon_searcher.live_submit.run_live_cycle", return_value=report) as cycle, \
             patch("hackathon_searcher.live_submit.print_live_report"):
            state = daily.run_current_data_preflight()

        self.assertEqual(state["ready_to_apply"], [candidate])
        self.assertIsNotNone(cycle.call_args.kwargs["submission_executor"])
        self.assertTrue(cycle.call_args.kwargs["preflight"])

    def test_daily_pipeline_passes_scored_queue_into_live_cycle(self):
        candidate = {
            "event_name": "Regression Queue Event",
            "applicant_name": "Alex Builder",
            "event_score": 88.0,
            "fit_score": 91.0,
            "travel_support": "CONFIRMED_FLIGHTS",
            "application_url": "https://example.test/apply",
            "reason": "confirmed_travel; score",
        }
        called = {"count": 0}

        def fake_run_live_cycle(*, submission_executor=None, preflight=False):
            called["count"] += 1
            return {
                "queue": [candidate],
                "attempted": [],
                "submitted": 0,
                "status_unknown": 0,
                "blocked": 0,
                "deferred": 0,
                "builder_count": 0,
                "teammate_count": 0,
                "travel_support_count": 0,
            }

        with patch.object(daily, "acquire_run_lock", return_value=True), \
             patch.object(daily, "release_run_lock"), \
             patch.object(daily, "is_daily_run_active", return_value=False), \
             patch.object(daily, "init_db"), \
             patch.object(daily, "start_daily_run", return_value="test-run"), \
             patch.object(daily, "discover_events", return_value=[]), \
             patch.object(daily, "process_discovered_events", return_value=([], [], 0)), \
             patch.object(daily, "get_events_needing_stage2_research", return_value=[]), \
             patch.object(daily, "rank_events_for_research", return_value=[]), \
             patch("hackathon_searcher.database.get_events_needing_research", return_value=[]), \
             patch.object(daily, "_write_daily_report", return_value="test-report"), \
             patch.object(daily, "complete_daily_run"), \
             patch.object(daily.settings, "DRY_RUN", False), \
             patch.object(daily.settings, "LIVE_TEST_MODE", True), \
             patch("hackathon_searcher.live_submit.prepare_ready_to_apply", return_value=[candidate]), \
             patch("hackathon_searcher.live_submit.run_live_cycle", side_effect=fake_run_live_cycle), \
             patch("hackathon_searcher.live_submit.print_live_report"), \
             patch("builtins.print"):
            state = daily.run_daily_pipeline()

        self.assertEqual(called["count"], 1)
        self.assertEqual(state["live_report"]["queue"][0]["event_name"], "Regression Queue Event")
        self.assertEqual(state["live_report"]["queue"][0]["application_url"], "https://example.test/apply")

    def test_live_cycle_accepts_queue_and_uses_submission_disabled_executor(self):
        candidate = {
            "event_id": "evt_regression",
            "event_name": "Regression Queue Event",
            "applicant_id": "builder",
            "applicant_name": "Alex Builder",
            "priority": 279,
            "reason": "confirmed_travel; score",
            "event_score": 88.0,
            "fit_score": 91.0,
            "travel_support": "CONFIRMED_FLIGHTS",
            "application_url": "https://example.test/apply",
        }
        profile = SimpleNamespace(applicant_id="builder", name="Alex Builder", email="builder@example.test", google_oauth_email="")
        event = {
            "event_id": "evt_regression",
            "event_name": "Regression Queue Event",
            "event_score": 88.0,
            "travel_support": "CONFIRMED_FLIGHTS",
            "application_url": "https://example.test/apply",
            "extra_data": "{}",
            "status": "DISCOVERED",
            "themes": "[]",
            "organizer": "Test Organizer",
            "description": "Test event",
            "city": "Stockholm",
            "country": "SE",
        }
        executor_calls = []
        fields = [SimpleNamespace(label="Email", field_type="email", answer="")]

        def submission_disabled_executor(*args):
            executor_calls.append(args)
            return {
                "status": "BLOCKED",
                "submit_clicked": False,
                "confirmation_text": "",
                "confirmation_url": "",
                "confirmation_reference": "",
                "error": "submission-disabled preflight",
            }

        with patch.object(live_submit, "build_live_submission_queue", return_value=[candidate]), \
             patch.object(live_submit.profile_manager, "get", return_value=profile), \
             patch.object(live_submit, "get_event_by_id", return_value=event), \
             patch.object(live_submit, "get_application", return_value={
                 "status": "READY_TO_APPLY", "application_url": candidate["application_url"],
                 "form_provider": "custom", "form_fillable": 1, "application_open": 1,
                 "fact_check_passed": 1, "cross_profile_check_passed": 1,
                 "duplicate_check_passed": 1, "questions": [{"label": "Email", "field_type": "email", "required": True}],
                 "answers": [{"label": "Email", "answer": profile.email}],
             }), \
             patch.object(live_submit, "has_application", return_value=True), \
             patch.object(live_submit, "validate_live_eligibility", return_value={
                 "passes": True,
                 "checks": {"no_duplicate": True},
                 "blockers": [],
                 "fit_score": 91.0,
                 "event_score": 88.0,
             }), \
             patch("hackathon_searcher.event_research.fetch_page", return_value="<form></form>"), \
             patch.object(live_submit, "extract_form_fields", return_value=fields), \
             patch.object(live_submit, "generate_answer_for_field", return_value=profile.email), \
             patch.object(live_submit, "validate_application", return_value=[]), \
             patch.object(live_submit, "create_submission_snapshot", return_value="preflight-snapshot.json"), \
             patch.object(live_submit, "log_audit"), \
             patch.object(live_submit.settings, "MAX_LIVE_APPLICATIONS_PER_RUN", 3):
            report = live_submit.run_live_cycle(submission_executor=submission_disabled_executor)

        self.assertEqual(len(executor_calls), 1)
        self.assertEqual(report["queue"][0]["event_name"], "Regression Queue Event")
        self.assertEqual(len(report["attempted"]), 1)
        self.assertFalse(report["attempted"][0]["submit_clicked"])
        self.assertEqual(report["attempted"][0]["final_status"], "BLOCKED")

    def test_only_complete_ready_records_enter_queue(self):
        records = [
            {"status": "QUALIFIED", "application_url": "", "event_id": "a", "applicant_id": "builder"},
            {"status": "READY_TO_APPLY", "application_url": "", "event_id": "no-url", "applicant_id": "builder", "form_provider": "custom", "eligibility_status": "ELIGIBLE", "event_score": 80, "applicant_fit_score": 80, "questions": [{"label": "Email"}], "answers": [{"label": "Email", "answer": "alex@example.test"}], "fact_check_passed": 1, "cross_profile_check_passed": 1, "duplicate_check_passed": 1, "form_fillable": 1, "application_open": 1},
            {"status": "READY_TO_APPLY", "application_url": "https://example.test", "event_id": "no-answers", "applicant_id": "builder", "form_provider": "custom", "eligibility_status": "ELIGIBLE", "event_score": 80, "applicant_fit_score": 80, "questions": [], "answers": [], "fact_check_passed": 1, "cross_profile_check_passed": 1, "duplicate_check_passed": 1, "form_fillable": 1, "application_open": 1},
            {"status": "READY_TO_APPLY", "application_url": "https://example.test", "event_id": "bad-fact", "applicant_id": "builder", "form_provider": "custom", "eligibility_status": "ELIGIBLE", "event_score": 80, "applicant_fit_score": 80, "questions": [{"label": "Email"}], "answers": [{"label": "Email", "answer": "wrong@example.com"}], "fact_check_passed": 0, "cross_profile_check_passed": 1, "duplicate_check_passed": 1, "form_fillable": 1, "application_open": 1},
            {"status": "READY_TO_APPLY", "application_url": "https://example.test", "event_id": "b", "applicant_id": "builder", "application_group_id": "team_b", "form_provider": "custom", "eligibility_status": "ELIGIBLE", "event_score": 80, "applicant_fit_score": 80, "apply_score": 48, "questions": [{"label": "Email"}], "answers": [{"label": "Email", "answer": "alex@example.test"}], "fact_check_passed": 1, "cross_profile_check_passed": 1, "duplicate_check_passed": 1, "consent_policy_passed": 1, "unreadable_required_fields": 0, "form_validation_status": "VALIDATED", "form_fillable": 1, "application_open": 1},
            {"status": "READY_TO_APPLY", "application_url": "https://example.test", "event_id": "b", "applicant_id": "teammate", "application_group_id": "team_b", "form_provider": "custom", "eligibility_status": "ELIGIBLE", "event_score": 80, "applicant_fit_score": 80, "apply_score": 64, "questions": [{"label": "Email"}], "answers": [{"label": "Email", "answer": "sam@example.test"}], "fact_check_passed": 1, "cross_profile_check_passed": 1, "duplicate_check_passed": 1, "consent_policy_passed": 1, "unreadable_required_fields": 0, "form_validation_status": "VALIDATED", "form_fillable": 1, "application_open": 1},
        ]
        event = {"event_id": "b", "event_name": "Ready", "event_score": 80, "final_travel_status": "UNKNOWN", "team_status": "TEAM_READY_TO_APPLY", "team_apply_score": 56, "team_application_group_id": "team_b"}
        profile = SimpleNamespace(name="Alex Builder")
        with patch.object(live_submit, "get_all_applications", return_value=records), \
             patch.object(live_submit, "get_event_by_id", return_value=event), \
             patch.object(live_submit.profile_manager, "get", return_value=profile), \
             patch.object(live_submit, "load_team", return_value=TeamConfig("test", ("builder", "teammate"), 55)), \
             patch.object(live_submit, "_update_blocked_application"):
            queue = live_submit.build_live_submission_queue()
        self.assertEqual(len(queue), 2)
        self.assertEqual(queue[0]["event_id"], "b")

    def test_team_below_threshold_never_enters_the_individual_queue(self):
        apps = [
            {"event_id": "below", "applicant_id": "builder", "status": "READY_TO_APPLY", "application_group_id": "team_below"},
            {"event_id": "below", "applicant_id": "teammate", "status": "READY_TO_APPLY", "application_group_id": "team_below"},
        ]
        event = {"event_id": "below", "team_status": "TEAM_BELOW_THRESHOLD", "team_apply_score": 53.0, "team_application_group_id": "team_below"}
        with patch.object(live_submit, "get_all_applications", return_value=apps), \
             patch.object(live_submit, "get_event_by_id", return_value=event), \
             patch.object(live_submit, "_update_blocked_application"):
            self.assertEqual(live_submit.build_live_submission_queue(), [])

    def test_verified_normal_email_and_oauth_separation(self):
        builder = SimpleNamespace(email="alex@example.test", google_oauth_email="")
        builder_field = SimpleNamespace(label="Email", name="email", field_type="email", required=True, answer=builder.email)
        self.assertEqual(live_submit.validate_factual_consistency([builder_field], [{"label": "Email", "answer": builder.email}], builder), [])
        profile = SimpleNamespace(email="sam@example.test", google_oauth_email="oauth@example.test")
        normal = SimpleNamespace(label="Email", name="email", field_type="email", required=True, answer=profile.email)
        oauth = SimpleNamespace(label="Email", name="email", field_type="email", required=True, answer=profile.google_oauth_email)
        self.assertEqual(live_submit.validate_factual_consistency([normal], [{"label": "Email", "answer": profile.email}], profile), [])
        self.assertTrue(live_submit.validate_factual_consistency([oauth], [{"label": "Email", "answer": profile.google_oauth_email}], profile))
        self.assertEqual(live_submit.validate_factual_consistency([oauth], [{"label": "Email", "answer": profile.google_oauth_email}], profile, context="authentication"), [])

    def test_profile_email_is_used_for_the_application_channel(self):
        profile = SimpleNamespace(email="applicant@example.test", google_oauth_email="oauth@example.test")
        field = SimpleNamespace(label="Email", name="email", field_type="email", required=True, answer=profile.email)
        self.assertEqual(live_submit.validate_factual_consistency([field], [{"label": "Email", "answer": profile.email}], profile), [])

    def test_luma_public_email_gate_is_not_classified_as_auth_required(self):
        """A visible Luma sign-in affordance must not block public registration."""
        browser = Mock()
        browser.page.url = "https://luma.com/example-event"
        browser.get_page_text.return_value = "Example event\nSign In\nRegister"
        browser.get_page_html.return_value = "<input type='email'>"
        browser.is_captcha_present.return_value = False
        browser.is_google_oauth_required.return_value = False
        browser.find_form.return_value = True
        browser.page.query_selector_all.return_value = []

        email_field = live_submit.FormField(
            label="Email", name="email", field_type="email", required=True,
        )
        with patch.object(live_submit, "extract_form_fields", return_value=[email_field]), \
             patch.object(live_submit, "_form_is_actionable", return_value=True):
            result = live_submit._inspect_form_page(browser, browser.page.url)

        self.assertEqual(result["status"], "PUBLIC_EMAIL_GATE")
        self.assertFalse(result["metadata"]["login_required"])

    def test_luma_click_and_confirmation_phases_are_audited_separately(self):
        """A confirmation timeout must never erase evidence of a completed click."""
        fields = [
            live_submit.FormField(label="Name *", name="name", field_type="text", required=True),
            live_submit.FormField(label="Email *", name="email", field_type="email", required=True),
        ]
        profile = SimpleNamespace(email="alex@example.test")
        page = MagicMock()
        button = MagicMock()
        button_details = {"form_count": 1, "button_count": 1, "visible": True, "enabled": True, "top_element": {"same": True}}
        browser = MagicMock()
        browser.page = page
        browser.fill_field.return_value = True
        session = MagicMock()
        session.__enter__.return_value = browser
        audit_actions = []

        def record_audit(_event_id, action, *args, **kwargs):
            audit_actions.append(action)

        with patch.object(live_submit.profile_manager, "get", return_value=profile), \
             patch.object(live_submit, "get_event_by_id", return_value={"event_url": "https://luma.com/example"}), \
             patch.object(live_submit, "get_application", return_value={"application_group_id": "team_example"}), \
             patch.object(live_submit, "BrowserSession", return_value=session), \
             patch.object(live_submit, "_open_luma_public_form", return_value=(fields, {}, "")), \
             patch.object(live_submit, "_luma_active_submit_button", return_value=(button, button_details)), \
             patch.object(live_submit, "_luma_button_diagnostics", return_value=button_details), \
             patch.object(live_submit, "detect_confirmation", return_value={"confirmed": False, "text": "", "url": "https://luma.com/example", "reference": ""}), \
             patch.object(live_submit, "update_application"), \
             patch.object(live_submit, "log_audit", side_effect=record_audit):
            result = live_submit._execute_luma_live_submission(
                "evt_example", "builder", fields,
                [{"label": "Name *", "answer": "Alex Builder"}, {"label": "Email *", "answer": profile.email}],
                {"passes": True}, "snapshot.json",
            )

        self.assertTrue(result["submit_clicked"])
        self.assertEqual(result["click_phase"], "CLICK_COMPLETED")
        self.assertEqual(result["status"], "SUBMITTED_CONFIRMATION_UNKNOWN")
        self.assertEqual(audit_actions[:7], [
            "BUTTON_FOUND", "BUTTON_SCROLLED_INTO_VIEW", "BUTTON_ACTIONABLE",
            "SUBMIT_INTENT_RECORDED", "CLICK_START", "SUBMIT_CLICK_ISSUED",
            "SUBMIT_CLICK_COMPLETED",
        ])

    def test_stale_luma_auth_blocker_is_rediscovered(self):
        event = {"event_url": "https://luma.com/example-event"}
        existing = {
            "status": "AUTH_SETUP_REQUIRED",
            "discovery_status": "APPLICATION_LOGIN_REQUIRED",
            "application_url": "https://luma.com/example-event",
        }
        self.assertTrue(live_submit._should_refresh_discovery(event, existing))

    def test_luma_domain_has_a_named_form_provider(self):
        self.assertEqual(live_submit.detect_form_provider("https://luma.com/example-event"), "luma")

    def test_luma_is_human_assisted_while_other_providers_remain_autonomous(self):
        self.assertEqual(human_assist.provider_capability("luma"), human_assist.HUMAN_ASSISTED)
        self.assertEqual(human_assist.provider_capability("typeform"), human_assist.AUTONOMOUS_SUPPORTED)

    def test_qualified_luma_queue_is_diverted_to_a_human_package(self):
        queue = [
            {"event_id": "luma-event", "form_provider": "luma"},
            {"event_id": "luma-event", "form_provider": "luma"},
            {"event_id": "other-event", "form_provider": "typeform"},
        ]
        with patch.object(human_assist, "create_human_assist_package", return_value={"event": "human_assist/event.txt"}) as package:
            result = human_assist.prepare_luma_human_assist_from_queue(queue)
        package.assert_called_once_with("luma-event")
        self.assertEqual(result, [{"event_id": "luma-event", "paths": {"event": "human_assist/event.txt"}}])

    def test_form_fill_validation_refuses_to_run_outside_dry_run(self):
        with patch.object(live_submit.settings, "DRY_RUN", False):
            with self.assertRaises(RuntimeError):
                live_submit.validate_luma_form_fill(())

    def test_fit_theme_expansion_splits_compound_technical_tags(self):
        from hackathon_searcher.scoring import _expanded_event_themes
        themes = _expanded_event_themes(["defense-security"], "AI and robotics prototypes")
        self.assertTrue({"defense", "security", "ai", "robotics"}.issubset(themes))

    def test_consent_policy_accepts_required_rules_but_refuses_optional_marketing(self):
        from hackathon_searcher.forms import consent_policy_for_field
        required = live_submit.FormField(label="Do you accept the security guidelines? *", required=False)
        marketing = live_submit.FormField(label="I agree to receive marketing communications", required=True)
        self.assertEqual(consent_policy_for_field(required), ("Yes", "REQUIRED_EVENT_RULES"))
        self.assertEqual(consent_policy_for_field(marketing), ("No", "OPTIONAL_MARKETING"))
        talent = live_submit.FormField(label="May we share your profile with our talent pool?", required=False)
        opted_in = SimpleNamespace(data={"consent": {"accept_optional_talent_pool": True}})
        opted_out = SimpleNamespace(data={"consent": {"accept_optional_talent_pool": False}})
        self.assertEqual(consent_policy_for_field(talent, opted_in), ("Yes", "OPTIONAL_TALENT_POOL"))
        self.assertEqual(consent_policy_for_field(talent, opted_out), ("No", "OPTIONAL_TALENT_POOL"))

    def test_required_unknown_fields_are_not_guessed_but_user_confirmed_discovery_source_is_used(self):
        from hackathon_searcher.forms import generate_answer_for_field
        profile = SimpleNamespace(email="applicant@example.com", match_answer=lambda *_: None)
        unknown = live_submit.FormField(label="", name="", required=True)
        discovery = live_submit.FormField(label="How did you hear about this event?", required=True)
        self.assertEqual(generate_answer_for_field(unknown, profile, use_llm=False), "UNKNOWN_REQUIRED_FIELD")
        self.assertEqual(generate_answer_for_field(discovery, profile, use_llm=False), "HackathonHub.eu")

    def test_required_dropdown_without_a_verified_matching_option_is_blocked(self):
        from hackathon_searcher.forms import generate_answer_for_field
        profile = SimpleNamespace(email="applicant@example.com", match_answer=lambda *_: None)
        field = live_submit.FormField(label="Select your professional role", field_type="dropdown", required=True, options=["Engineer", "Student"])
        self.assertEqual(generate_answer_for_field(field, profile, use_llm=False), "UNKNOWN_REQUIRED_FIELD")

    def test_custom_dropdown_without_options_never_gets_prose_answer(self):
        from hackathon_searcher.forms import generate_answer_for_field
        profile = SimpleNamespace(email="applicant@example.com", match_answer=lambda *_: None)
        field = live_submit.FormField(
            label="What is your current role? *", field_type="text",
            placeholder="Välj ett alternativ", required=False,
        )
        with patch("hackathon_searcher.forms.generate_application_answer") as generate:
            self.assertEqual(generate_answer_for_field(field, profile), "UNKNOWN_REQUIRED_FIELD")
            generate.assert_not_called()

    def test_cli_import_does_not_replace_standard_output(self):
        """CLI commands must keep a usable stdout stream for preflight reporting."""
        import sys
        original_stdout = sys.stdout
        import hackathon_searcher.cli  # noqa: F401
        self.assertIs(sys.stdout, original_stdout)

    def test_missing_application_id_is_derived_per_event_and_applicant(self):
        from hackathon_searcher.database import insert_application
        with patch("hackathon_searcher.database.get_connection") as get_connection:
            conn = Mock()
            conn.execute.return_value = Mock(lastrowid=1)
            get_connection.return_value = conn
            insert_application({"event_id": "evt_example", "applicant_id": "teammate", "applicant_name": "Sam"})
        _, params = conn.execute.call_args.args
        self.assertEqual(params[0], "app_evt_example_teammate")


if __name__ == "__main__":
    unittest.main()
