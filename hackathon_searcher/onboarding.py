"""Small interactive onboarding and progressive profile enrichment."""

from __future__ import annotations

import json
import re
from pathlib import Path

from hackathon_searcher.profile import profile_manager
from hackathon_searcher.settings import settings
from hackathon_searcher.team import MAX_TEAM_SIZE, TeamConfig, save_team


def _ask(label: str, default: str = "", required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            value = input(f"{label}{suffix}: ").strip() or default
        except EOFError as exc:
            raise ValueError("Setup was interrupted before all answers were provided. Rerun `setup` to start again.") from exc
        if value or not required:
            return value
        print("This value is required.")


def _yes_no(label: str, default: bool = False) -> bool:
    default_text = "Y/n" if default else "y/N"
    answer = _ask(f"{label} ({default_text})").lower()
    return default if not answer else answer in {"y", "yes", "ja"}


def _items(label: str, limit: int = 0) -> list[str]:
    value = _ask(label)
    values = [item.strip() for item in value.split(",") if item.strip()]
    return values[:limit] if limit else values


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return result or "applicant"


def _profile_path(applicant_id: str) -> Path:
    return Path(settings.PROFILES_DIR) / f"{applicant_id}.json"


def _answer_path(applicant_id: str) -> Path:
    return Path(settings.ANSWER_LIBRARY_DIR) / f"{applicant_id}.json"


def _write_profile(data: dict) -> None:
    _profile_path(data["applicant_id"]).parent.mkdir(parents=True, exist_ok=True)
    _answer_path(data["applicant_id"]).parent.mkdir(parents=True, exist_ok=True)
    _profile_path(data["applicant_id"]).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if not _answer_path(data["applicant_id"]).exists():
        _answer_path(data["applicant_id"]).write_text('{"answers": []}\n', encoding="utf-8")


def collect_profile(index: int) -> dict:
    print(f"\nApplicant {index}")
    full_name = _ask("Full name", required=True)
    applicant_id = _slug(_ask("Short applicant ID", _slug(full_name), required=True))
    email = _ask("Application email", required=True)
    age_text = _ask("Age", required=True)
    while not age_text.isdigit() or int(age_text) < 1:
        age_text = _ask("Age (whole number)", required=True)
    city = _ask("City", required=True)
    country = _ask("Country", required=True)
    role = _ask("Current role or status (e.g. student, employed, self-employed)", required=True)
    is_student = _yes_no("Are you currently a student?")
    education = _ask("Education or school", "") if is_student else ""
    interests = _items("Major interests (comma-separated)")
    topics = _items("Preferred hackathon topics (comma-separated)")
    projects = []
    for project_index in range(1, 4):
        project = _ask(f"Project/experience {project_index} (blank to stop)")
        if not project:
            break
        projects.append({"name": project, "description": _ask("  Short description"), "role": role})
    regions = _items("Travel regions you can attend (comma-separated, e.g. Europe)")
    travel = _ask("Travel support: required / preferred / not_required", "preferred").lower()
    if travel not in {"required", "preferred", "not_required"}:
        travel = "preferred"
    return {
        "schema_version": 2, "applicant_id": applicant_id, "full_name": full_name, "name": full_name,
        "email": email, "age": int(age_text), "location": {"city": city, "country": country},
        "home_city": city, "home_country": country, "travel_origin": city,
        "employment_status": role, "education": {"school": education, "current_student": is_student},
        "interests": interests, "preferred_hackathon_topics": topics, "projects": projects,
        "travel_regions": regions, "travel_support_preference": travel,
        "travel_support_wanted": travel != "not_required", "skills": [],
        "linkedin": _ask("LinkedIn URL (optional)"), "github": _ask("GitHub URL (optional)"),
        "website": _ask("Website URL (optional)"), "portfolio": "",
        "consent": {"auto_apply": False, "share_personal_info": False, "request_travel_support": travel != "not_required"},
        "availability": {"weekends": True, "weekdays": False, "can_attend_full_event_duration": True},
    }


def configure_llm() -> None:
    supported = {"openai", "anthropic", "gemini", "mistral", "openai_compatible"}
    while True:
        provider = _ask("LLM provider (openai / anthropic / gemini / mistral / openai_compatible)", "openai").lower()
        if provider in supported:
            break
        print("Choose one of: openai, anthropic, gemini, mistral, openai_compatible.")
    api_key = _ask("API key (stored only in local .env)", required=True)
    model = _ask("Model", required=True)
    base_url = _ask("Base URL (only for openai_compatible)") if provider == "openai_compatible" else ""
    path = Path(".env")
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    values = {"LLM_PROVIDER": provider, "LLM_API_KEY": api_key, "LLM_MODEL": model, "LLM_BASE_URL": base_url}
    retained = [line for line in existing.splitlines() if not any(line.startswith(f"{key}=") for key in values)]
    path.write_text("\n".join(retained + [f"{key}={value}" for key, value in values.items()]) + "\n", encoding="utf-8")


def run_setup() -> TeamConfig:
    print("Hackathon Searcher setup — local files only")
    is_team = _yes_no("Are you applying as a team?")
    count = 2 if is_team else 1
    if is_team:
        count_text = _ask(f"Number of applicants (2-{MAX_TEAM_SIZE})", "2")
        count = int(count_text) if count_text.isdigit() else 2
        count = min(MAX_TEAM_SIZE, max(2, count))
    profiles = [collect_profile(index) for index in range(1, count + 1)]
    if len({profile["applicant_id"] for profile in profiles}) != len(profiles):
        raise ValueError("Applicant IDs must be unique")
    threshold_text = _ask("Minimum team score", "55")
    threshold = float(threshold_text) if threshold_text.replace(".", "", 1).isdigit() else 55.0
    team = TeamConfig("default", tuple(profile["applicant_id"] for profile in profiles), threshold)
    configure_llm()
    # Write only once every interactive step has completed, so a cancelled
    # setup never leaves a half-created public user's configuration behind.
    for profile in profiles:
        _write_profile(profile)
    save_team(team)
    profile_manager.reload()
    return team


def improve_profile(applicant_id: str) -> dict:
    path = _profile_path(applicant_id)
    if not path.exists():
        raise ValueError("Applicant profile not found")
    data = json.loads(path.read_text(encoding="utf-8"))
    print("Add only facts you are comfortable using in applications. Leave a prompt blank to keep its current value.")
    projects = data.setdefault("projects", [])
    while len(projects) < 3:
        name = _ask("Add a project (blank to continue)")
        if not name:
            break
        projects.append({"name": name, "description": _ask("  Description"), "role": _ask("  Your role")})
    fields = (
        ("work_experience", "Work experience (comma-separated)"),
        ("startup_experience", "Startup or founder experience (comma-separated)"),
        ("hackathon_experience", "Hackathon experience (comma-separated)"),
        ("achievements", "Awards or achievements (comma-separated)"),
        ("metrics", "Verified metrics or outcomes (comma-separated)"),
        ("communities", "Communities or clubs (comma-separated)"),
        ("skills", "Skills (comma-separated)"),
        ("personal_facts_allowed", "Personal facts you allow in applications (comma-separated)"),
    )
    for key, label in fields:
        if values := _items(label):
            data[key] = values
    if facts := _items("Facts or topics you do not want used (comma-separated)"):
        data["do_not_use_facts"] = facts
    _write_profile(data)
    profile_manager.reload()
    return data


def validate_profile(applicant_id: str) -> list[str]:
    profile = profile_manager.get(applicant_id)
    if not profile:
        return ["Profile not found"]
    required = {"applicant_id": profile.applicant_id, "full name": profile.full_name, "application email": profile.email, "age": profile.age, "city": profile.city, "country": profile.country}
    issues = [f"Missing {name}" for name, value in required.items() if not value]
    if profile.email and "@" not in profile.email:
        issues.append("Application email must contain @")
    if profile.age and not 1 <= profile.age <= 120:
        issues.append("Age must be between 1 and 120")
    return issues
