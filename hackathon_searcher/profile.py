"""
Profile management for Philip's verified facts.

Loads profile.json and answer_library.json.
Provides semantic matching for application questions.
All answers are derived from verified facts only — no hallucinations.
"""

import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional


class Profile:
    """Centralized profile with all verified facts about Philip."""

    def __init__(self, profile_path: str = "profile.json", answer_library_path: str = "answer_library.json"):
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

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    @property
    def name(self) -> str:
        return self.data.get("name", "")

    @property
    def city(self) -> str:
        return self.data.get("location", {}).get("city", "")

    @property
    def country(self) -> str:
        return self.data.get("location", {}).get("country", "")

    @property
    def age(self) -> int:
        return self.data.get("age", 0)

    @property
    def education(self) -> str:
        return self.data.get("education", "")

    @property
    def interests(self) -> list[str]:
        return self.data.get("interests", [])

    @property
    def hackathon_experience(self) -> list[str]:
        return self.data.get("hackathon_experience", [])

    @property
    def products(self) -> list[dict]:
        return self.data.get("products", [])

    @property
    def work_experience(self) -> list[dict]:
        return self.data.get("work_experience", [])

    @property
    def contact(self) -> dict:
        return self.data.get("contact", {})

    @property
    def travel_preferences(self) -> dict:
        return self.data.get("travel_preferences", {})

    @property
    def consent(self) -> dict:
        return self.data.get("consent", {})

    @property
    def requires_travel_support(self) -> bool:
        return self.travel_preferences.get("requires_travel_support", False)

    @property
    def willing_to_travel(self) -> bool:
        return self.travel_preferences.get("willing_to_travel", True)

    def match_answer(self, question: str, event_context: Optional[dict] = None) -> Optional[dict]:
        """Semantically match a question to the answer library. Returns {answer, id, confidence} or None."""
        question_lower = question.lower().strip()
        best_match: Optional[dict] = None
        best_score = 0.0

        for entry in self.answers:
            for pattern in entry.get("question_patterns", []):
                score = SequenceMatcher(None, question_lower, pattern.lower()).ratio()
                # Boost for substring matches
                if pattern.lower() in question_lower:
                    score = max(score, 0.85)
                if score > best_score:
                    best_score = score
                    best_match = {"id": entry["id"], "answer": entry["answer"], "confidence": score}

        if best_match and best_match["confidence"] >= 0.5:
            return best_match
        return None

    def get_tailored_answer(self, question: str, event_context: Optional[dict] = None) -> str:
        """
        Get an answer tailored to the event context.
        Uses the answer library as a base, then adjusts for context.
        """
        match = self.match_answer(question, event_context)
        if not match:
            return ""

        answer = match["answer"]

        if not event_context:
            return answer

        # Add event-specific tailoring
        themes = event_context.get("themes", [])
        if isinstance(themes, str):
            try:
                themes = json.loads(themes)
            except (json.JSONDecodeError, TypeError):
                themes = []

        event_name = event_context.get("event_name", "")

        # Tailor "why hackathon" for specific themes
        if match["id"] == "why_hackathon":
            theme_str = ", ".join(themes[:2]) if themes else "technology and building"
            answer = answer.replace(
                "This hackathon's focus aligns with the kind of products I want to create",
                f"I'm drawn to {event_name} because its focus on {theme_str} aligns with what I love building"
            )

        # Tailor "why accept me" for specific themes
        if match["id"] == "why_accept_me":
            if any(t.lower() in ["ai", "artificial intelligence"] for t in themes):
                answer += " I'm particularly excited about building AI products that solve real problems."
            elif any(t.lower() in ["hardware", "robotics", "drones"] for t in themes):
                answer += " My robotics and CNC background gives me a practical edge I bring to hardware projects."

        return answer

    def has_fact(self, key_path: str) -> bool:
        """Check if a specific fact exists in the profile. e.g. 'contact.email'."""
        parts = key_path.split(".")
        current = self.data
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return False
        return bool(current) if not isinstance(current, (dict, list)) else bool(current)

    def get_fact(self, key_path: str, default=None):
        """Get a specific fact by dotted path."""
        parts = key_path.split(".")
        current = self.data
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
        return current


profile = Profile()
