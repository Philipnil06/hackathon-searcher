"""Manual, applicant-separated browser authentication management.

Authentication is performed by the user in a headed Chromium profile. The
profile directory contains browser-managed session data; this module never
asks for, reads, prints, or serializes passwords, OTPs, or access tokens.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hackathon_searcher.browser import BrowserSession
from hackathon_searcher.profile import profile_manager
from hackathon_searcher.settings import settings


PLATFORM_TARGETS: dict[str, dict[str, str]] = {
    "junction": {
        "url": "https://hackjunction.app/auth/signin?redirect_url=https%3A%2F%2Fhackjunction.app%2Fevents%2Fjunction-2026%2Fregistration",
        "label": "Hack Junction",
    },
}


def _profile_dir(applicant_id: str) -> Path:
    return Path(settings.BROWSER_PROFILES_DIR) / applicant_id


def _status_path(applicant_id: str) -> Path:
    return _profile_dir(applicant_id) / "auth_status.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_status(applicant_id: str) -> dict[str, Any]:
    path = _status_path(applicant_id)
    if not path.exists():
        return {"applicant_id": applicant_id, "platforms": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"applicant_id": applicant_id, "platforms": {}}
    except (OSError, json.JSONDecodeError):
        return {"applicant_id": applicant_id, "platforms": {}}


def _write_status(applicant_id: str, data: dict[str, Any]) -> None:
    path = _status_path(applicant_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # This metadata contains only status/timestamps/URLs, never browser secrets.
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _auth_detected(browser: BrowserSession, platform: str, applicant_id: str) -> bool:
    try:
        url = (browser.page.url or "").lower()
        body = browser.get_page_text().lower()
        if any(marker in body for marker in ("log out", "sign out", "my account", "my profile")):
            return True
        if platform == "junction" and re.search(r"\b(account|profile|dashboard)\b", body):
            return True
        profile = profile_manager.get(applicant_id)
        if profile and profile.email.lower() in body:
            return True
        if any(marker in url for marker in ("/login", "/signin", "/sign-in", "/auth/")):
            return False
        if any(marker in body for marker in ("sign in", "log in", "continue with google")):
            return False
        if platform == "luma" and url.startswith("https://luma.com/"):
            return True
        if platform == "junction" and url.startswith("https://hackjunction.app/"):
            return True
        return False
    except Exception:
        return False


def _save_platform_status(
    applicant_id: str,
    platform: str,
    status: str,
    login_url: str,
    reason: str = "",
) -> None:
    data = _read_status(applicant_id)
    data["applicant_id"] = applicant_id
    data.setdefault("platforms", {})[platform] = {
        "status": status,
        "login_url": login_url,
        "last_checked": _now(),
        "reason": reason,
    }
    _write_status(applicant_id, data)


def run_auth_setup(applicant_id: str) -> dict[str, Any]:
    """Open a headed persistent profile and wait for manual authentication."""
    if applicant_id not in profile_manager.applicant_ids:
        return {"status": "AUTH_FAILED", "reason": f"Unknown applicant: {applicant_id}"}

    profile = profile_manager.get(applicant_id)
    print(f"AUTH SETUP: {profile.name if profile else applicant_id.upper()}")
    print("Credentials, OAuth, email verification, 2FA and CAPTCHA must be completed manually in the browser.")
    print("The agent will not enter or store passwords, OTPs, or tokens.")

    results: dict[str, str] = {}
    try:
        with BrowserSession(applicant_id=applicant_id, headless=False, persistent=True, channel="chrome") as browser:
            for platform, target in PLATFORM_TARGETS.items():
                print(f"\nOpen {target['label']}: {target['url']}")
                if not browser.navigate(target["url"]):
                    results[platform] = "AUTH_FAILED"
                    _save_platform_status(applicant_id, platform, "AUTH_FAILED", target["url"], "Navigation failed")
                    continue
                input(f"Complete {target['label']} authentication in the visible browser, then press Enter here: ")
                if not browser.navigate(target["url"]):
                    results[platform] = "AUTH_FAILED"
                    _save_platform_status(applicant_id, platform, "AUTH_FAILED", target["url"], "Browser closed before validation")
                    print(f"{target['label']}: AUTH_FAILED")
                    continue
                if _auth_detected(browser, platform, applicant_id):
                    results[platform] = "AUTHENTICATED"
                    _save_platform_status(applicant_id, platform, "AUTHENTICATED", target["url"])
                    print(f"{target['label']}: AUTH_SUCCESS")
                else:
                    results[platform] = "AUTH_FAILED"
                    _save_platform_status(applicant_id, platform, "AUTH_FAILED", target["url"], "Logged-in state was not detected")
                    print(f"{target['label']}: AUTH_FAILED")
    except Exception as exc:
        return {"status": "AUTH_FAILED", "results": results, "reason": str(exc)[:200]}

    overall = "AUTH_SUCCESS" if all(value == "AUTHENTICATED" for value in results.values()) else "AUTH_FAILED"
    print(f"\n{overall}")
    return {"status": overall, "results": results}


def validate_saved_auth(applicant_id: str) -> dict[str, str]:
    """Validate saved browser sessions without exposing session data."""
    data = _read_status(applicant_id)
    if not _profile_dir(applicant_id).exists():
        return {platform: "NOT_CONFIGURED" for platform in PLATFORM_TARGETS}

    result: dict[str, str] = {}
    for platform, target in PLATFORM_TARGETS.items():
        saved = data.get("platforms", {}).get(platform, {})
        if saved.get("status") != "AUTHENTICATED":
            result[platform] = saved.get("status", "NOT_CONFIGURED")
            continue
        try:
            with BrowserSession(applicant_id=applicant_id, headless=True, persistent=True) as browser:
                browser.navigate(target["url"])
                valid = _auth_detected(browser, platform, applicant_id)
            result[platform] = "AUTHENTICATED" if valid else "AUTH_EXPIRED"
            _save_platform_status(applicant_id, platform, result[platform], target["url"])
        except Exception:
            result[platform] = "AUTH_EXPIRED"
            _save_platform_status(applicant_id, platform, "AUTH_EXPIRED", target["url"], "Validation failed")
    return result


def mark_auth_expired(applicant_id: str, platform: str, login_url: str = "") -> None:
    _save_platform_status(applicant_id, platform, "AUTH_EXPIRED", login_url, "Manual authentication setup required again")


def print_auth_status() -> None:
    for applicant_id in profile_manager.applicant_ids:
        profile = profile_manager.get(applicant_id)
        print(f"\n{profile.name if profile else applicant_id.upper()}")
        statuses = validate_saved_auth(applicant_id)
        print(f"Hack Junction: {statuses.get('junction', 'NOT_CONFIGURED')}")
        print("Google OAuth: NOT_REQUIRED")
