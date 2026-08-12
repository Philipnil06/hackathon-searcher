"""Non-submitting local readiness checks used by the public preflight command."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from hackathon_searcher.database import init_db
from hackathon_searcher.dedicated_browser import EXTENSION_DIR, bridge_connected, dedicated_profile_path, find_chrome, is_dedicated_profile_safe
from hackathon_searcher.llm import llm_configuration_status
from hackathon_searcher.onboarding import validate_profile
from hackathon_searcher.profile import profile_manager
from hackathon_searcher.scheduler import scheduler_status
from hackathon_searcher.settings import settings
from hackathon_searcher.team import load_team


def local_preflight_checks() -> dict[str, Any]:
    """Inspect configuration and local storage; never access an LLM or submit."""
    result: dict[str, Any] = {"profiles": [], "team": None, "issues": [], "warnings": []}
    profile_manager.reload()
    try:
        team = load_team()
        result["team"] = {"id": team.team_id, "members": list(team.members), "minimum_score": team.minimum_team_score}
        for applicant_id in team.members:
            issues = validate_profile(applicant_id)
            result["profiles"].append({"applicant_id": applicant_id, "valid": not issues, "issues": issues})
            if issues:
                result["issues"].append(f"Profile {applicant_id}: " + "; ".join(issues))
    except (OSError, ValueError) as exc:
        result["issues"].append(f"Team configuration: {exc}")

    try:
        init_db()
        result["database"] = {"ready": True, "path": settings.DATABASE_PATH}
    except Exception as exc:  # keep expected setup errors readable
        result["database"] = {"ready": False, "path": settings.DATABASE_PATH}
        result["issues"].append(f"Database: {exc}")

    result["env_file"] = Path(".env").is_file()
    result["llm"] = llm_configuration_status()
    if not result["llm"]["ready"]:
        result["issues"].append(result["llm"]["message"])

    profile = dedicated_profile_path()
    result["browser"] = {
        "optional": True,
        "chrome_found": bool(find_chrome()),
        "profile": str(profile),
        "profile_safe": is_dedicated_profile_safe(profile),
        "extension_source": EXTENSION_DIR.is_dir(),
        "bridge_running": bridge_connected(),
    }
    if not result["browser"]["chrome_found"]:
        result["warnings"].append("Chrome is optional. Install it only if you want Luma autofill.")

    try:
        result["scheduler"] = scheduler_status()
    except RuntimeError as exc:
        result["scheduler"] = {"supported": True, "exists": False, "message": str(exc)}
        result["warnings"].append("Could not read Task Scheduler status; daily runs can still be started manually.")
    return result
