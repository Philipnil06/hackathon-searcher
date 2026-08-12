"""
Multi-applicant profile management.

Loads profiles from profiles/ directory and answer libraries from answer_library/.
Each applicant gets their own verified facts and answer library.
"""

import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from hackathon_searcher.settings import settings


class ApplicantProfile:
    """A single applicant's verified profile and answer library."""

    def __init__(self, profile_path: str, answer_library_path: str):
        self.profile_path = Path(profile_path)
        self.answer_library_path = Path(answer_library_path)
        self.data: dict = {}
        self.answers: list[dict] = []
        self._load()

    def _load(self) -> None:
        if self.profile_path.exists():
            with open(self.profile_path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        if self.answer_library_path.exists():
            with open(self.answer_library_path, "r", encoding="utf-8") as f:
                lib = json.load(f)
                self.answers = lib.get("answers", [])

    def reload(self) -> None:
        self._load()

    @property
    def applicant_id(self) -> str:
        return self.data.get("applicant_id", "")

    @property
    def name(self) -> str:
        return self.data.get("name", "")

    @property
    def full_name(self) -> str:
        return self.data.get("full_name", self.data.get("name", ""))

    @property
    def email(self) -> str:
        return self.data.get("email", "")

    @property
    def google_oauth_email(self) -> str:
        return self.data.get("google_oauth_email", self.data.get("email", ""))

    @property
    def phone(self) -> str:
        return self.data.get("phone", "")

    @property
    def city(self) -> str:
        return self.data.get("location", {}).get("city", self.data.get("home_city", ""))

    @property
    def country(self) -> str:
        return self.data.get("location", {}).get("country", self.data.get("home_country", ""))

    @property
    def home_city(self) -> str:
        return self.data.get("home_city", "")

    @property
    def home_country(self) -> str:
        return self.data.get("home_country", "")

    @property
    def travel_origin(self) -> str:
        return self.data.get("travel_origin", self.home_city)

    @property
    def age(self) -> int:
        return self.data.get("age", 0)

    @property
    def education(self) -> dict:
        return self.data.get("education", {})

    @property
    def education_text(self) -> str:
        edu = self.education
        if edu.get("degree"):
            return edu["degree"]
        if edu.get("english_description"):
            return edu["english_description"]
        return ""

    @property
    def current_student(self) -> bool:
        return self.education.get("current_student", False)

    @property
    def is_university_student(self) -> bool:
        return self.education.get("is_university_student", False)

    @property
    def is_high_school_student(self) -> bool:
        return self.education.get("is_high_school_student", False)

    @property
    def linkedin(self) -> str:
        return self.data.get("linkedin", "")

    @property
    def github(self) -> str:
        return self.data.get("github", "")

    @property
    def website(self) -> str:
        return self.data.get("website", "")

    @property
    def portfolio(self) -> str:
        return self.data.get("portfolio", "")

    @property
    def interests(self) -> list[str]:
        return self.data.get("interests", [])

    @property
    def projects(self) -> list[dict]:
        return self.data.get("projects", [])

    @property
    def work_experience(self) -> list[dict]:
        return self.data.get("work_experience", [])

    @property
    def hackathon_experience(self) -> list:
        return self.data.get("hackathon_experience", [])

    @property
    def achievements(self) -> list[dict]:
        return self.data.get("achievements", [])

    @property
    def growth_experience(self) -> dict:
        return self.data.get("growth_experience", {})

    @property
    def travel_support_wanted(self) -> bool:
        return self.data.get("travel_support_wanted", False)

    @property
    def dietary_requirements(self) -> str:
        return self.data.get("dietary_requirements", "")

    @property
    def auth_type(self) -> str:
        return self.data.get("auth", {}).get("type", "standard")

    @property
    def skills(self) -> list[str]:
        return self.data.get("skills", [])

    @property
    def default_partner(self) -> str:
        return self.data.get("team", {}).get("default_partner", "")

    def match_answer(self, question: str, event_context: Optional[dict] = None) -> Optional[dict]:
        """Semantically match a question to the answer library."""
        question_lower = question.lower().strip()
        best_match: Optional[dict] = None
        best_score = 0.0

        for entry in self.answers:
            for pattern in entry.get("question_patterns", []):
                score = SequenceMatcher(None, question_lower, pattern.lower()).ratio()
                if pattern.lower() in question_lower:
                    score = max(score, 0.85)
                if score > best_score:
                    best_score = score
                    best_match = {"id": entry["id"], "answer": entry["answer"], "confidence": score}

        if best_match and best_match["confidence"] >= 0.5:
            return best_match
        return None

    def has_fact(self, key_path: str) -> bool:
        parts = key_path.split(".")
        current = self.data
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return False
        return bool(current) if not isinstance(current, (dict, list)) else bool(current)

    def get_fact(self, key_path: str, default=None):
        parts = key_path.split(".")
        current = self.data
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
        return current


class ProfileManager:
    """Manages multiple applicant profiles."""

    def __init__(self):
        self._profiles: dict[str, ApplicantProfile] = {}
        self._load_profiles()

    def _load_profiles(self) -> None:
        profiles_dir = Path(settings.PROFILES_DIR)
        if not profiles_dir.exists():
            return

        for profile_file in profiles_dir.glob("*.json"):
            if profile_file.stem.startswith("example_"):
                continue
            applicant_id = profile_file.stem
            answer_lib_path = Path(settings.ANSWER_LIBRARY_DIR) / f"{applicant_id}.json"
            self._profiles[applicant_id] = ApplicantProfile(
                str(profile_file), str(answer_lib_path)
            )

    def reload(self) -> None:
        self._profiles.clear()
        self._load_profiles()

    def get(self, applicant_id: str) -> Optional[ApplicantProfile]:
        return self._profiles.get(applicant_id)

    @property
    def applicant_ids(self) -> list[str]:
        return list(self._profiles.keys())

    @property
    def all_profiles(self) -> list[ApplicantProfile]:
        return list(self._profiles.values())

    def get_profile_data(self, applicant_id: str) -> dict:
        """Get raw profile data for LLM context."""
        profile = self.get(applicant_id)
        return profile.data if profile else {}

    def get_answer_library(self, applicant_id: str) -> list[dict]:
        """Get answer library entries for LLM context."""
        profile = self.get(applicant_id)
        return profile.answers if profile else []


# Global instance
profile_manager = ProfileManager()
