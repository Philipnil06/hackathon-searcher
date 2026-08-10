"""
Centralized settings for the Hackathon Searcher agent.

All configurable thresholds and preferences live here.
Loaded from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # --- Application behavior ---
    AUTO_APPLY: bool = os.getenv("AUTO_APPLY", "true").lower() == "true"
    DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"
    MIN_APPLICATION_SCORE: int = int(os.getenv("MIN_APPLICATION_SCORE", "55"))
    AUTO_APPLY_FLIGHT_SUPPORT: bool = os.getenv("AUTO_APPLY_FLIGHT_SUPPORT", "true").lower() == "true"

    # --- Discovery ---
    HACKATHON_HUB_URL: str = os.getenv("HACKATHON_HUB_URL", "https://hackathonhub.eu/")
    CRAWL_INTERVAL_HOURS: int = int(os.getenv("CRAWL_INTERVAL_HOURS", "24"))

    # --- Geography ---
    HOME_CITY: str = os.getenv("HOME_CITY", "Stockholm")
    HOME_COUNTRY: str = os.getenv("HOME_COUNTRY", "Sweden")
    PREFER_PHYSICAL_EVENTS: bool = os.getenv("PREFER_PHYSICAL_EVENTS", "true").lower() == "true"
    PREFERRED_REGIONS: list[str] = field(default_factory=lambda: ["Europe"])

    # --- Profile ---
    PROFILE_PATH: str = os.getenv("PROFILE_PATH", "profile.json")
    ANSWER_LIBRARY_PATH: str = os.getenv("ANSWER_LIBRARY_PATH", "answer_library.json")

    # --- Database ---
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "hackathon_searcher.db")

    # --- Browser ---
    HEADLESS: bool = os.getenv("HEADLESS", "true").lower() == "true"
    BROWSER_TIMEOUT_MS: int = int(os.getenv("BROWSER_TIMEOUT_MS", "30000"))

    # --- LLM ---
    LLM_MODEL: str = os.getenv("LLM_MODEL", "")

    # --- Notification ---
    NOTIFY_HIGH_SCORE_THRESHOLD: int = int(os.getenv("NOTIFY_HIGH_SCORE_THRESHOLD", "80"))
    NOTIFY_ON_APPLICATION: bool = os.getenv("NOTIFY_ON_APPLICATION", "true").lower() == "true"
    NOTIFY_ON_FLIGHT_SUPPORT: bool = os.getenv("NOTIFY_ON_FLIGHT_SUPPORT", "true").lower() == "true"
    NOTIFY_ON_BLOCKED: bool = os.getenv("NOTIFY_ON_BLOCKED", "true").lower() == "true"

    # --- Debug ---
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    @property
    def is_dry_run(self) -> bool:
        return self.DRY_RUN

    @property
    def should_auto_apply(self) -> bool:
        return self.AUTO_APPLY and not self.DRY_RUN


settings = Settings()
