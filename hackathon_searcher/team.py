"""Local, configurable team management for one to four applicants."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from hackathon_searcher.settings import settings

MAX_TEAM_SIZE = 4


@dataclass(frozen=True)
class TeamConfig:
    team_id: str
    members: tuple[str, ...]
    minimum_team_score: float = 55.0

    @classmethod
    def from_dict(cls, data: dict) -> "TeamConfig":
        members = tuple(str(member).strip() for member in data.get("members", []) if str(member).strip())
        if not 1 <= len(members) <= MAX_TEAM_SIZE:
            raise ValueError(f"A team must have between 1 and {MAX_TEAM_SIZE} members")
        if len(set(members)) != len(members):
            raise ValueError("A team cannot contain the same applicant more than once")
        return cls(str(data.get("team_id") or "default"), members, float(data.get("minimum_team_score", 55)))

    def to_dict(self) -> dict:
        return {"team_id": self.team_id, "members": list(self.members), "minimum_team_score": self.minimum_team_score}


def team_path() -> Path:
    return Path(settings.TEAM_CONFIG_PATH)


def load_team() -> TeamConfig:
    """Load local team configuration, migrating legacy configured members once."""
    path = team_path()
    if path.exists():
        return TeamConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))
    profiles_dir = Path(settings.PROFILES_DIR)
    members = tuple(sorted(path.stem for path in profiles_dir.glob("*.json") if not path.stem.startswith("example_")))
    # This one-time fallback preserves existing local installations that used
    # profile files before team.json existed. The generated configuration stays
    # local because team.json is ignored.
    team = TeamConfig("default", members or tuple(settings.TEAM_APPLICANT_IDS), settings.TEAM_MIN_APPLY_SCORE)
    if members:
        save_team(team)
    return team


def save_team(team: TeamConfig) -> None:
    path = team_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(team.to_dict(), indent=2) + "\n", encoding="utf-8")


def add_member(applicant_id: str) -> TeamConfig:
    team = load_team()
    from hackathon_searcher.profile import profile_manager
    profile_manager.reload()
    if not profile_manager.get(applicant_id):
        raise ValueError(f"Applicant profile '{applicant_id}' was not found. Run `profile show` to see available IDs.")
    if applicant_id in team.members:
        raise ValueError("That applicant is already on the team")
    if len(team.members) >= MAX_TEAM_SIZE:
        raise ValueError(f"A team can have at most {MAX_TEAM_SIZE} members")
    updated = TeamConfig(team.team_id, (*team.members, applicant_id), team.minimum_team_score)
    save_team(updated)
    return updated


def remove_member(applicant_id: str) -> TeamConfig:
    team = load_team()
    members = tuple(member for member in team.members if member != applicant_id)
    if len(members) == len(team.members):
        raise ValueError("Applicant is not on the team")
    if not members:
        raise ValueError("A team needs at least one member")
    updated = TeamConfig(team.team_id, members, team.minimum_team_score)
    save_team(updated)
    return updated
