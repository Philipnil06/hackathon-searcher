import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hackathon_searcher import llm, scheduler
from hackathon_searcher.onboarding import configure_llm, run_setup
from hackathon_searcher.team import TeamConfig


class PublicOnboardingTests(unittest.TestCase):
    def test_cli_help_lists_public_onboarding_commands(self):
        result = subprocess.run(
            [sys.executable, "-m", "hackathon_searcher.cli", "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        for command in ("setup", "preflight", "daily", "profile", "team", "llm", "browser", "schedule"):
            self.assertIn(command, result.stdout)

    def test_llm_status_never_exposes_a_key(self):
        with patch.object(llm.settings, "LLM_PROVIDER", "openai"), \
             patch.object(llm.settings, "LLM_MODEL", "example-model"), \
             patch("hackathon_searcher.llm._provider_key", return_value="not-for-output"):
            result = llm.llm_configuration_status()
        self.assertTrue(result["ready"])
        self.assertNotIn("not-for-output", " ".join(str(value) for value in result.values()))

    def test_llm_status_explains_missing_model(self):
        with patch.object(llm.settings, "LLM_PROVIDER", "openai"), patch.object(llm.settings, "LLM_MODEL", ""):
            result = llm.llm_configuration_status()
        self.assertFalse(result["ready"])
        self.assertIn("model is missing", result["message"])

    def test_scheduler_status_decodes_actual_shape_without_writing(self):
        payload = json.dumps({"exists": True, "task_name": "Hackathon Searcher Daily", "state": "Ready"})
        with patch.object(scheduler, "is_windows", return_value=True), patch.object(scheduler, "_powershell", return_value=payload):
            result = scheduler.scheduler_status()
        self.assertTrue(result["supported"])
        self.assertTrue(result["exists"])

    def test_scheduler_remove_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(RuntimeError, "schedule remove --yes"):
            scheduler.remove_scheduler()

    def test_fictional_team_schema_supports_one_and_four_people(self):
        self.assertEqual(TeamConfig.from_dict({"members": ["alex_builder"]}).members, ("alex_builder",))
        team = TeamConfig.from_dict({"members": ["alex_builder", "sam_maker", "jordan_dev", "riley_design"]})
        self.assertEqual(len(team.members), 4)

    def test_setup_can_store_each_supported_provider_without_a_network_call(self):
        providers = ("openai", "anthropic", "gemini", "mistral", "openai_compatible")
        original_directory = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                for provider in providers:
                    answers = [provider, "fictional-key", "fictional-model"]
                    if provider == "openai_compatible":
                        answers.append("https://example.invalid/v1")
                    with patch("builtins.input", side_effect=answers):
                        configure_llm()
                    env_text = Path(".env").read_text(encoding="utf-8")
                    self.assertIn(f"LLM_PROVIDER={provider}", env_text)
                    self.assertIn("LLM_MODEL=fictional-model", env_text)
                    self.assertNotIn("OPENAI_API_KEY=fictional-key", env_text)
            finally:
                os.chdir(original_directory)

    def test_solo_setup_creates_location_and_travel_preferences_without_json_edits(self):
        answers = [
            "n", "Alex Builder", "", "alex@example.test", "25", "Dublin", "Ireland", "builder", "n",
            "AI", "AI", "", "1", "1", "1", "n", "", "", "", "55",
            "openai", "fictional-key", "fictional-model",
        ]
        original_directory = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                with patch.object(__import__("hackathon_searcher.onboarding", fromlist=["settings"]).settings, "PROFILES_DIR", "profiles"), \
                     patch.object(__import__("hackathon_searcher.onboarding", fromlist=["settings"]).settings, "ANSWER_LIBRARY_DIR", "answer_library"), \
                     patch.object(__import__("hackathon_searcher.onboarding", fromlist=["settings"]).settings, "TEAM_CONFIG_PATH", "team.json"), \
                     patch("builtins.input", side_effect=answers):
                    team = run_setup()
                profile = json.loads(Path("profiles/alex_builder.json").read_text(encoding="utf-8"))
                self.assertEqual(team.members, ("alex_builder",))
                self.assertEqual(profile["location"], {"city": "Dublin", "country": "Ireland"})
                self.assertEqual(profile["travel_preferences"]["scope"], "city")
                self.assertFalse(profile["travel_preferences"]["include_remote"])
            finally:
                os.chdir(original_directory)

    def test_team_setup_creates_two_profiles_and_local_team_config(self):
        answers = [
            "y", "2",
            "Alex Builder", "", "alex@example.test", "25", "Dublin", "Ireland", "builder", "n", "AI", "AI", "", "1", "1", "1", "n", "", "", "",
            "Sam Maker", "", "sam@example.test", "26", "Paris", "France", "designer", "n", "product", "product", "", "3", "3", "1,3", "150", "2", "n", "", "", "",
            "55", "mistral", "fictional-key", "fictional-model",
        ]
        original_directory = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                with patch.object(__import__("hackathon_searcher.onboarding", fromlist=["settings"]).settings, "PROFILES_DIR", "profiles"), \
                     patch.object(__import__("hackathon_searcher.onboarding", fromlist=["settings"]).settings, "ANSWER_LIBRARY_DIR", "answer_library"), \
                     patch.object(__import__("hackathon_searcher.onboarding", fromlist=["settings"]).settings, "TEAM_CONFIG_PATH", "team.json"), \
                     patch("builtins.input", side_effect=answers):
                    team = run_setup()
                teammate = json.loads(Path("profiles/sam_maker.json").read_text(encoding="utf-8"))
                self.assertEqual(team.members, ("alex_builder", "sam_maker"))
                self.assertEqual(teammate["travel_preferences"]["scope"], "region")
                self.assertEqual(teammate["travel_preferences"]["minimum_reimbursement_eur"], 150.0)
            finally:
                os.chdir(original_directory)


if __name__ == "__main__":
    unittest.main()
