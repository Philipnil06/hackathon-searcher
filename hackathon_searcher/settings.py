"""
Centralized settings for the Hackathon Searcher agent.

All configurable thresholds and preferences live here.
Loaded from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

# Load .env file if it exists
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_path)
    except ImportError:
        pass


@dataclass
class Settings:
    # --- Application behavior ---
    AUTO_APPLY: bool = os.getenv("AUTO_APPLY", "false").lower() == "true"
    DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"
    MIN_APPLICATION_SCORE: int = int(os.getenv("MIN_APPLICATION_SCORE", "55"))
    AUTO_APPLY_FLIGHT_SUPPORT: bool = os.getenv("AUTO_APPLY_FLIGHT_SUPPORT", "true").lower() == "true"
    TEAM_APPLICANT_IDS: tuple[str, ...] = tuple(
        applicant_id.strip() for applicant_id in os.getenv("TEAM_APPLICANT_IDS", "builder,teammate").split(",")
        if applicant_id.strip()
    )
    TEAM_CONFIG_PATH: str = os.getenv("TEAM_CONFIG_PATH", "team.json")
    TEAM_MIN_APPLY_SCORE: float = float(os.getenv("TEAM_MIN_APPLY_SCORE", "55"))

    # --- Live test mode ---
    LIVE_TEST_MODE: bool = os.getenv("LIVE_TEST_MODE", "false").lower() == "true"
    MAX_LIVE_APPLICATIONS_PER_RUN: int = int(os.getenv("MAX_LIVE_APPLICATIONS_PER_RUN", "3"))
    LIVE_MIN_EVENT_SCORE: int = int(os.getenv("LIVE_MIN_EVENT_SCORE", "60"))
    LIVE_MIN_APPLY_SCORE: int = int(os.getenv("LIVE_MIN_APPLY_SCORE", "60"))
    LIVE_REQUIRE_CONFIRMED_TRAVEL: bool = os.getenv("LIVE_REQUIRE_CONFIRMED_TRAVEL", "false").lower() == "true"
    STOP_AFTER_FIRST_SUCCESS: bool = os.getenv("STOP_AFTER_FIRST_SUCCESS", "false").lower() == "true"

    # --- Deterministic consent policies ---
    ACCEPT_REQUIRED_EVENT_RULES: bool = os.getenv("ACCEPT_REQUIRED_EVENT_RULES", "true").lower() == "true"
    ACCEPT_REQUIRED_DATA_PROCESSING: bool = os.getenv("ACCEPT_REQUIRED_DATA_PROCESSING", "true").lower() == "true"
    ACCEPT_OPTIONAL_NEWSLETTER: bool = os.getenv("ACCEPT_OPTIONAL_NEWSLETTER", "false").lower() == "true"
    ACCEPT_OPTIONAL_MARKETING: bool = os.getenv("ACCEPT_OPTIONAL_MARKETING", "false").lower() == "true"
    ACCEPT_OPTIONAL_TALENT_POOL: bool = os.getenv("ACCEPT_OPTIONAL_TALENT_POOL", "false").lower() == "true"
    ACCEPT_OPTIONAL_MEDIA: bool = os.getenv("ACCEPT_OPTIONAL_MEDIA", "false").lower() == "true"

    # --- Discovery ---
    HACKATHON_HUB_URL: str = os.getenv("HACKATHON_HUB_URL", "https://hackathonhub.eu/")
    CRAWL_INTERVAL_HOURS: int = int(os.getenv("CRAWL_INTERVAL_HOURS", "24"))
    DAILY_STAGE2_CAP: int = int(os.getenv("DAILY_STAGE2_CAP", "5"))
    DAILY_MAX_RUNTIME_MINUTES: int = int(os.getenv("DAILY_MAX_RUNTIME_MINUTES", "25"))

    # --- Geography ---
    # Legacy global values; runtime location preferences are profile-specific.
    HOME_CITY: str = os.getenv("HOME_CITY", "")
    HOME_COUNTRY: str = os.getenv("HOME_COUNTRY", "")
    PREFER_PHYSICAL_EVENTS: bool = os.getenv("PREFER_PHYSICAL_EVENTS", "true").lower() == "true"
    PREFERRED_REGIONS: list[str] = field(default_factory=lambda: ["Europe"])

    # --- Profile ---
    PROFILES_DIR: str = os.getenv("PROFILES_DIR", "profiles")
    ANSWER_LIBRARY_DIR: str = os.getenv("ANSWER_LIBRARY_DIR", "answer_library")
    # --- Database ---
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "hackathon_searcher.db")

    # --- Browser ---
    HEADLESS: bool = os.getenv("HEADLESS", "true").lower() == "true"
    BROWSER_TIMEOUT_MS: int = int(os.getenv("BROWSER_TIMEOUT_MS", "30000"))
    BROWSER_PROFILES_DIR: str = os.getenv("BROWSER_PROFILES_DIR", "browser_profiles")

    # --- LLM ---
    LLM_MODEL: str = os.getenv("LLM_MODEL", "")
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "openai").lower()
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "")

    # --- Luma extension auto-submit ---
    # When DRY_RUN=false AND this is true, the Chrome extension may click the
    # final Luma submit button after filling and report the confirmation back
    # to the local bridge, which records the application as APPLIED.
    LUMA_AUTO_SUBMIT: bool = os.getenv("LUMA_AUTO_SUBMIT", "false").lower() == "true"
    LUMA_CONFIRMATION_TIMEOUT_MS: int = int(os.getenv("LUMA_CONFIRMATION_TIMEOUT_MS", "9000"))

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

    @property
    def should_auto_submit_luma(self) -> bool:
        """Only allow the extension to click Luma's final submit in live mode."""
        return self.LUMA_AUTO_SUBMIT and not self.DRY_RUN


settings = Settings()
