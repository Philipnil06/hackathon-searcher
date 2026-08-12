"""
Database layer for Hackathon Searcher — multi-applicant edition.

Uses SQLite. Tracks events, per-applicant applications, application groups, and audit logs.
UNIQUE(event_id, applicant_id) prevents duplicate applications.
"""

import json
import hashlib
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hackathon_searcher.settings import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    fingerprint TEXT UNIQUE NOT NULL,
    event_name TEXT NOT NULL,
    organizer TEXT DEFAULT '',
    hackathonhub_url TEXT DEFAULT '',
    event_url TEXT DEFAULT '',
    application_url TEXT DEFAULT '',
    city TEXT DEFAULT '',
    country TEXT DEFAULT '',
    venue TEXT DEFAULT '',
    physical_or_online TEXT DEFAULT 'unknown',
    start_date TEXT DEFAULT '',
    end_date TEXT DEFAULT '',
    application_deadline TEXT DEFAULT '',
    application_open INTEGER DEFAULT 1,
    description TEXT DEFAULT '',
    themes TEXT DEFAULT '[]',
    sponsors TEXT DEFAULT '[]',
    judges TEXT DEFAULT '[]',
    partners TEXT DEFAULT '[]',
    prizes TEXT DEFAULT '[]',
    participant_limit TEXT DEFAULT '',
    age_requirement TEXT DEFAULT '',
    student_requirement TEXT DEFAULT '',
    nationality_requirement TEXT DEFAULT '',
    eligibility_rules_raw TEXT DEFAULT '',
    travel_support TEXT DEFAULT 'UNKNOWN',
    travel_support_type TEXT DEFAULT '',
    travel_support_amount TEXT DEFAULT '',
    travel_support_currency TEXT DEFAULT '',
    travel_support_confidence REAL DEFAULT 0.0,
    travel_support_probability REAL DEFAULT 0.0,
    travel_support_source TEXT DEFAULT '',
    hub_travel_status TEXT DEFAULT '',
    official_travel_status TEXT DEFAULT '',
    final_travel_status TEXT DEFAULT '',
    application_open_date TEXT DEFAULT '',
    last_application_check TEXT DEFAULT '',
    next_application_check TEXT DEFAULT '',
    travel_support_details TEXT DEFAULT '{}',
    flight_credits TEXT DEFAULT '',
    accommodation TEXT DEFAULT '',
    food TEXT DEFAULT '',
    event_score REAL DEFAULT 0.0,
    team_status TEXT DEFAULT '',
    team_member_scores TEXT DEFAULT '{}',
    team_apply_score REAL DEFAULT 0.0,
    team_member_travel_eligibility TEXT DEFAULT '{}',
    team_travel_status TEXT DEFAULT '',
    team_application_group_id TEXT DEFAULT '',
    score_reasoning TEXT DEFAULT '',
    confidence REAL DEFAULT 0.0,
    notes TEXT DEFAULT '',
    date_discovered TEXT NOT NULL,
    date_last_checked TEXT NOT NULL,
    status TEXT DEFAULT 'DISCOVERED',
    extra_data TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT UNIQUE NOT NULL,
    event_id TEXT NOT NULL,
    applicant_id TEXT NOT NULL,
    applicant_name TEXT NOT NULL,
    application_url TEXT DEFAULT '',
    application_group_id TEXT DEFAULT '',
    questions TEXT DEFAULT '[]',
    answers TEXT DEFAULT '[]',
    date_started TEXT DEFAULT '',
    date_submitted TEXT DEFAULT '',
    application_confirmation TEXT DEFAULT '',
    application_reference TEXT DEFAULT '',
    submission_snapshot TEXT DEFAULT '{}',
    status TEXT DEFAULT 'DISCOVERED',
    event_score REAL DEFAULT 0.0,
    applicant_fit_score REAL DEFAULT 0.0,
    travel_score REAL DEFAULT 0.0,
    apply_score REAL DEFAULT 0.0,
    eligibility_status TEXT DEFAULT '',
    eligibility_reasoning TEXT DEFAULT '',
    travel_eligible INTEGER DEFAULT 0,
    travel_support_requested INTEGER DEFAULT 0,
    location_fit_score REAL DEFAULT 0.0,
    location_status TEXT DEFAULT '',
    travel_requirement_status TEXT DEFAULT '',
    accommodation_requirement_status TEXT DEFAULT '',
    score_reasoning TEXT DEFAULT '',
    travel_support_status TEXT DEFAULT '',
    form_provider TEXT DEFAULT '',
    application_discovery_source TEXT DEFAULT '',
    application_discovery_confidence REAL DEFAULT 0.0,
    application_discovery_reason TEXT DEFAULT '',
    application_discovery_path TEXT DEFAULT '[]',
    discovery_status TEXT DEFAULT '',
    application_open_date TEXT DEFAULT '',
    last_application_check TEXT DEFAULT '',
    next_application_check TEXT DEFAULT '',
    auth_status TEXT DEFAULT '',
    auth_platform TEXT DEFAULT '',
    auth_login_url TEXT DEFAULT '',
    auth_account TEXT DEFAULT '',
    source_pages_checked TEXT DEFAULT '[]',
    form_metadata TEXT DEFAULT '{}',
    form_fillable INTEGER DEFAULT 0,
    application_open INTEGER DEFAULT 0,
    fact_check_passed INTEGER DEFAULT 0,
    cross_profile_check_passed INTEGER DEFAULT 0,
    duplicate_check_passed INTEGER DEFAULT 0,
    consent_policy_passed INTEGER DEFAULT 0,
    unreadable_required_fields INTEGER DEFAULT 0,
    form_validation_status TEXT DEFAULT '',
    form_validated_at TEXT DEFAULT '',
    eligibility_confidence REAL DEFAULT 0.0,
    eligibility_source_evidence TEXT DEFAULT '',
    eligibility_requirements TEXT DEFAULT '[]',
    notes TEXT DEFAULT '',
    FOREIGN KEY (event_id) REFERENCES events(event_id),
    UNIQUE(event_id, applicant_id)
);

CREATE TABLE IF NOT EXISTS application_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT UNIQUE NOT NULL,
    event_id TEXT NOT NULL,
    applicant_ids TEXT DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES events(event_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_id TEXT DEFAULT '',
    applicant_id TEXT DEFAULT '',
    action TEXT NOT NULL,
    detail TEXT DEFAULT '',
    level TEXT DEFAULT 'INFO'
);

CREATE TABLE IF NOT EXISTS crawl_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    last_crawl TEXT NOT NULL,
    events_scanned INTEGER DEFAULT 0,
    events_new INTEGER DEFAULT 0,
    events_updated INTEGER DEFAULT 0,
    applications_submitted INTEGER DEFAULT 0,
    applications_blocked INTEGER DEFAULT 0,
    report TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS daily_run_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_started TEXT NOT NULL,
    run_completed TEXT DEFAULT '',
    status TEXT DEFAULT 'RUNNING',
    events_total INTEGER DEFAULT 0,
    events_new INTEGER DEFAULT 0,
    events_updated INTEGER DEFAULT 0,
    events_researched INTEGER DEFAULT 0,
    submitted_by_applicant TEXT DEFAULT '{}',
    applications_blocked INTEGER DEFAULT 0,
    travel_support_found INTEGER DEFAULT 0,
    errors TEXT DEFAULT '[]',
    report_path TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
CREATE INDEX IF NOT EXISTS idx_events_score ON events(event_score);
CREATE INDEX IF NOT EXISTS idx_events_country ON events(country);
CREATE INDEX IF NOT EXISTS idx_events_fingerprint ON events(fingerprint);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_applications_event_id ON applications(event_id);
CREATE INDEX IF NOT EXISTS idx_applications_applicant_id ON applications(applicant_id);
CREATE INDEX IF NOT EXISTS idx_applications_group ON applications(application_group_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_event_id ON audit_log(event_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_applicant_id ON audit_log(applicant_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);
"""


def get_db_path() -> Path:
    return Path(settings.DATABASE_PATH)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(get_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        _migrate_schema(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Add lifecycle columns to databases created by older versions."""
    migrations = {
        "events": {
            "hub_travel_status": "TEXT DEFAULT ''",
            "official_travel_status": "TEXT DEFAULT ''",
            "final_travel_status": "TEXT DEFAULT ''",
            "team_status": "TEXT DEFAULT ''",
            "team_member_scores": "TEXT DEFAULT '{}'",
            "team_apply_score": "REAL DEFAULT 0.0",
            "team_member_travel_eligibility": "TEXT DEFAULT '{}'",
            "team_travel_status": "TEXT DEFAULT ''",
            "team_application_group_id": "TEXT DEFAULT ''",
            "application_open_date": "TEXT DEFAULT ''",
            "last_application_check": "TEXT DEFAULT ''",
            "next_application_check": "TEXT DEFAULT ''",
        },
        "applications": {
            "form_provider": "TEXT DEFAULT ''",
            "application_discovery_source": "TEXT DEFAULT ''",
            "application_discovery_confidence": "REAL DEFAULT 0.0",
            "application_discovery_reason": "TEXT DEFAULT ''",
            "application_discovery_path": "TEXT DEFAULT '[]'",
            "discovery_status": "TEXT DEFAULT ''",
            "application_open_date": "TEXT DEFAULT ''",
            "last_application_check": "TEXT DEFAULT ''",
            "next_application_check": "TEXT DEFAULT ''",
            "auth_status": "TEXT DEFAULT ''",
            "auth_platform": "TEXT DEFAULT ''",
            "auth_login_url": "TEXT DEFAULT ''",
            "auth_account": "TEXT DEFAULT ''",
            "source_pages_checked": "TEXT DEFAULT '[]'",
            "form_metadata": "TEXT DEFAULT '{}'",
            "form_fillable": "INTEGER DEFAULT 0",
            "application_open": "INTEGER DEFAULT 0",
            "fact_check_passed": "INTEGER DEFAULT 0",
            "cross_profile_check_passed": "INTEGER DEFAULT 0",
            "duplicate_check_passed": "INTEGER DEFAULT 0",
            "eligibility_confidence": "REAL DEFAULT 0.0",
            "eligibility_source_evidence": "TEXT DEFAULT ''",
            "eligibility_requirements": "TEXT DEFAULT '[]'",
            "travel_score": "REAL DEFAULT 0.0",
            "apply_score": "REAL DEFAULT 0.0",
            "location_fit_score": "REAL DEFAULT 0.0",
            "location_status": "TEXT DEFAULT ''",
            "travel_requirement_status": "TEXT DEFAULT ''",
            "accommodation_requirement_status": "TEXT DEFAULT ''",
            "consent_policy_passed": "INTEGER DEFAULT 0",
            "unreadable_required_fields": "INTEGER DEFAULT 0",
            "form_validation_status": "TEXT DEFAULT ''",
            "form_validated_at": "TEXT DEFAULT ''",
        },
        "daily_run_state": {
            "submitted_by_applicant": "TEXT DEFAULT '{}'",
        },
    }
    for table, columns in migrations.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, definition in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


# --- Fingerprinting ---

def make_fingerprint(event_name: str, start_date: str, organizer: str = "", city: str = "") -> str:
    normalized_name = _normalize(event_name)
    normalized_organizer = _normalize(organizer or "")
    normalized_city = _normalize(city or "")
    raw = f"{normalized_name}|{start_date}|{normalized_organizer}|{normalized_city}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _normalize(s: str) -> str:
    return s.strip().lower().replace(" ", "").replace("-", "").replace("'", "").replace('"', "")


# --- Event CRUD ---

def event_exists_by_fingerprint(fingerprint: str) -> bool:
    conn = get_connection()
    try:
        row = conn.execute("SELECT 1 FROM events WHERE fingerprint = ?", (fingerprint,)).fetchone()
        return row is not None
    finally:
        conn.close()


def get_event_by_fingerprint(fingerprint: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM events WHERE fingerprint = ?", (fingerprint,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_event_by_id(event_id: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def insert_event(event: dict) -> str:
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        event.setdefault("date_discovered", now)
        event.setdefault("date_last_checked", now)
        event.setdefault("status", "DISCOVERED")
        event.setdefault("themes", "[]")
        event.setdefault("sponsors", "[]")
        event.setdefault("judges", "[]")
        event.setdefault("partners", "[]")
        event.setdefault("prizes", "[]")
        event.setdefault("travel_support_details", "{}")
        event.setdefault("extra_data", "{}")

        if "fingerprint" not in event:
            event["fingerprint"] = make_fingerprint(
                event.get("event_name", ""),
                event.get("start_date", ""),
                event.get("organizer", ""),
                event.get("city", "")
            )

        columns = [
            "event_id", "fingerprint", "event_name", "organizer", "hackathonhub_url",
            "event_url", "application_url", "city", "country", "venue",
            "physical_or_online", "start_date", "end_date", "application_deadline",
            "application_open", "description", "themes",
            "sponsors", "judges", "partners", "prizes", "participant_limit",
            "age_requirement", "student_requirement", "nationality_requirement",
            "eligibility_rules_raw",
            "travel_support", "travel_support_type", "travel_support_amount",
            "travel_support_currency", "travel_support_confidence",
            "travel_support_probability", "travel_support_source",
            "hub_travel_status", "official_travel_status", "final_travel_status",
            "application_open_date", "last_application_check", "next_application_check",
            "travel_support_details", "flight_credits", "accommodation", "food",
            "event_score", "score_reasoning", "confidence", "notes",
            "date_discovered", "date_last_checked", "status", "extra_data"
        ]

        values = [event.get(col, "") for col in columns]

        list_fields = {"themes", "sponsors", "judges", "partners", "prizes"}
        dict_fields = {"travel_support_details", "extra_data"}
        for i, col in enumerate(columns):
            if col in list_fields and not isinstance(values[i], str):
                values[i] = json.dumps(values[i])
            elif col in dict_fields and not isinstance(values[i], str):
                values[i] = json.dumps(values[i])

        placeholders = ", ".join(["?" for _ in columns])
        cols_str = ", ".join(columns)
        conn.execute(
            f"INSERT OR IGNORE INTO events ({cols_str}) VALUES ({placeholders})",
            values
        )
        conn.commit()
        return event["event_id"]
    finally:
        conn.close()


def update_event(event_id: str, updates: dict) -> bool:
    if not updates:
        return False
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        updates["date_last_checked"] = now

        list_fields = {"themes", "sponsors", "judges", "partners", "prizes"}
        dict_fields = {"travel_support_details", "extra_data"}
        for key, val in updates.items():
            if key in list_fields and not isinstance(val, str):
                updates[key] = json.dumps(val)
            elif key in dict_fields and not isinstance(val, str):
                updates[key] = json.dumps(val)

        set_clause = ", ".join([f"{k} = ?" for k in updates])
        values = list(updates.values()) + [event_id]
        conn.execute(f"UPDATE events SET {set_clause} WHERE event_id = ?", values)
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def get_all_events(status: Optional[str] = None) -> list[dict]:
    conn = get_connection()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM events WHERE status = ? ORDER BY event_score DESC",
                (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM events ORDER BY event_score DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_events_needing_research() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM events WHERE status IN ('DISCOVERED', 'RESEARCHING') ORDER BY date_discovered ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_events_ready_to_apply() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM events WHERE status = 'QUALIFIED' ORDER BY event_score DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def find_duplicate_events(event_name: str, start_date: str = "", city: str = "") -> list[dict]:
    conn = get_connection()
    try:
        normalized = _normalize(event_name)
        rows = conn.execute(
            "SELECT * FROM events WHERE LOWER(REPLACE(REPLACE(event_name, ' ', ''), '-', '')) LIKE ?",
            (f"%{normalized}%",)
        ).fetchall()
        results = [dict(r) for r in rows]
        if start_date:
            results = [r for r in results if r.get("start_date") == start_date]
        if city:
            results = [r for r in results if _normalize(r.get("city", "")) == _normalize(city)]
        return results
    finally:
        conn.close()


# --- Application CRUD (multi-applicant) ---

def has_application(event_id: str, applicant_id: str) -> bool:
    """Check if an application exists for this event + applicant."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM applications WHERE event_id = ? AND applicant_id = ? AND status NOT IN ('SKIPPED', 'INELIGIBLE')",
            (event_id, applicant_id)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_application(event_id: str, applicant_id: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM applications WHERE event_id = ? AND applicant_id = ?",
            (event_id, applicant_id)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_application_by_id(application_id: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM applications WHERE application_id = ?", (application_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def insert_application(app: dict) -> int:
    conn = get_connection()
    try:
        # Keep inserts idempotent per applicant/event.  An empty unique ID
        # combined with INSERT OR REPLACE can otherwise replace another
        # applicant's row and defeat cross-profile safety checks.
        if not app.get("application_id"):
            event_id = str(app.get("event_id", ""))
            applicant_id = str(app.get("applicant_id", ""))
            if not event_id or not applicant_id:
                raise ValueError("event_id and applicant_id are required for an application")
            app["application_id"] = f"app_{event_id[:12]}_{applicant_id}"
        list_fields = {"questions", "answers"}
        dict_fields = {"submission_snapshot", "form_metadata"}
        json_fields = {"source_pages_checked", "application_discovery_path", "eligibility_requirements"}
        for key, val in app.items():
            if (key in list_fields or key in json_fields) and not isinstance(val, str):
                app[key] = json.dumps(val)
            elif key in dict_fields and not isinstance(val, str):
                app[key] = json.dumps(val)

        columns = [
            "application_id", "event_id", "applicant_id", "applicant_name",
            "application_url", "application_group_id", "questions", "answers",
            "date_started", "date_submitted", "application_confirmation",
            "application_reference", "submission_snapshot", "status",
            "event_score", "applicant_fit_score", "travel_score", "apply_score", "eligibility_status",
            "eligibility_reasoning", "travel_eligible", "travel_support_requested",
            "location_fit_score", "location_status", "travel_requirement_status", "accommodation_requirement_status",
            "score_reasoning", "travel_support_status", "form_provider",
            "application_discovery_source", "application_discovery_confidence",
            "application_discovery_reason", "application_discovery_path", "discovery_status",
            "application_open_date", "last_application_check", "next_application_check",
            "auth_status", "auth_platform", "auth_login_url", "auth_account",
            "source_pages_checked", "form_metadata",
            "form_fillable", "application_open", "fact_check_passed",
            "cross_profile_check_passed", "duplicate_check_passed", "consent_policy_passed", "unreadable_required_fields",
            "form_validation_status", "form_validated_at",
            "eligibility_confidence", "eligibility_source_evidence",
            "eligibility_requirements", "notes"
        ]
        values = [app.get(col, "") for col in columns]
        placeholders = ", ".join(["?" for _ in columns])
        cols_str = ", ".join(columns)

        cursor = conn.execute(
            f"INSERT OR REPLACE INTO applications ({cols_str}) VALUES ({placeholders})",
            values
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_application(event_id: str, applicant_id: str, updates: dict) -> bool:
    conn = get_connection()
    try:
        list_fields = {"questions", "answers"}
        dict_fields = {"submission_snapshot", "form_metadata"}
        json_fields = {"source_pages_checked", "application_discovery_path", "eligibility_requirements"}
        for key, val in updates.items():
            if (key in list_fields or key in json_fields) and not isinstance(val, str):
                updates[key] = json.dumps(val)
            elif key in dict_fields and not isinstance(val, str):
                updates[key] = json.dumps(val)

        set_clause = ", ".join([f"{k} = ?" for k in updates])
        values = list(updates.values()) + [event_id, applicant_id]
        conn.execute(f"UPDATE applications SET {set_clause} WHERE event_id = ? AND applicant_id = ?", values)
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def get_all_applications(applicant_id: Optional[str] = None) -> list[dict]:
    conn = get_connection()
    try:
        if applicant_id:
            rows = conn.execute(
                "SELECT * FROM applications WHERE applicant_id = ? ORDER BY date_submitted DESC",
                (applicant_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM applications ORDER BY date_submitted DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_applications_by_status(status: str) -> list[dict]:
    """Return application records in one lifecycle status."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM applications WHERE status = ? ORDER BY event_score DESC, applicant_fit_score DESC",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_applications_for_event(event_id: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM applications WHERE event_id = ? ORDER BY applicant_id",
            (event_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Application groups ---

def create_application_group(group_id: str, event_id: str, applicant_ids: list[str]) -> None:
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO application_groups (group_id, event_id, applicant_ids, created_at) VALUES (?, ?, ?, ?)",
            (group_id, event_id, json.dumps(applicant_ids), now)
        )
        conn.commit()
    finally:
        conn.close()


# --- Audit log ---

def log_audit(event_id: str, action: str, detail: str = "", level: str = "INFO", applicant_id: str = "") -> None:
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO audit_log (timestamp, event_id, applicant_id, action, detail, level) VALUES (?, ?, ?, ?, ?, ?)",
            (now, event_id, applicant_id, action, detail, level)
        )
        conn.commit()
    finally:
        conn.close()


def get_audit_log(event_id: Optional[str] = None, applicant_id: Optional[str] = None, limit: int = 100) -> list[dict]:
    conn = get_connection()
    try:
        if event_id and applicant_id:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE event_id = ? AND applicant_id = ? ORDER BY timestamp DESC LIMIT ?",
                (event_id, applicant_id, limit)
            ).fetchall()
        elif event_id:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE event_id = ? ORDER BY timestamp DESC LIMIT ?",
                (event_id, limit)
            ).fetchall()
        elif applicant_id:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE applicant_id = ? ORDER BY timestamp DESC LIMIT ?",
                (applicant_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Crawl state ---

def save_crawl_state(state: dict) -> None:
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO crawl_state (last_crawl, events_scanned, events_new, events_updated, applications_submitted, applications_blocked, report) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, state.get("events_scanned", 0), state.get("events_new", 0),
             state.get("events_updated", 0), state.get("applications_submitted", 0),
             state.get("applications_blocked", 0), state.get("report", ""))
        )
        conn.commit()
    finally:
        conn.close()


def get_last_crawl() -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM crawl_state ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# --- Daily run state ---

def start_daily_run() -> int:
    """Start a new daily run. Returns the run ID."""
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            "INSERT INTO daily_run_state (run_started, status) VALUES (?, 'RUNNING')",
            (now,)
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def complete_daily_run(run_id: int, state: dict) -> None:
    """Mark a daily run as completed with summary data."""
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """UPDATE daily_run_state SET
               run_completed = ?, status = 'COMPLETED',
               events_total = ?, events_new = ?, events_updated = ?,
               events_researched = ?, submitted_by_applicant = ?, applications_blocked = ?,
               travel_support_found = ?, errors = ?, report_path = ?
               WHERE id = ?""",
            (now, state.get("events_total", 0), state.get("events_new", 0),
             state.get("events_updated", 0), state.get("events_researched", 0),
              json.dumps(state.get("submitted_by_applicant", {})),
             state.get("applications_blocked", 0), state.get("travel_support_found", 0),
             json.dumps(state.get("errors", [])), state.get("report_path", ""),
             run_id)
        )
        conn.commit()
    finally:
        conn.close()


def fail_daily_run(run_id: int, error: str) -> None:
    """Mark a daily run as failed."""
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE daily_run_state SET run_completed = ?, status = 'FAILED', errors = ? WHERE id = ?",
            (now, json.dumps([error]), run_id)
        )
        conn.commit()
    finally:
        conn.close()


def get_last_daily_run() -> Optional[dict]:
    """Get the most recent daily run."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM daily_run_state ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def is_daily_run_active() -> bool:
    """Check if a daily run is currently RUNNING (within last 30 minutes)."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT run_started FROM daily_run_state WHERE status = 'RUNNING' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return False
        started = datetime.fromisoformat(row["run_started"])
        now = datetime.now(timezone.utc)
        elapsed = (now - started).total_seconds()
        # If a run started more than 30 minutes ago, it's stale
        if elapsed > 1800:
            return False
        return True
    finally:
        conn.close()


def get_events_needing_stage2_research() -> list[dict]:
    """
    Get events that need external research:
    - NEW or RESEARCHING status
    - Or UPDATED with material changes
    - Plus events that pass Stage 1 scoring
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM events WHERE status IN ('DISCOVERED', 'RESEARCHING', 'UPDATED')
               OR (status = 'QUALIFIED' AND event_score > 0)
               ORDER BY event_score DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_events_with_pending_applications() -> list[dict]:
    """Get events where at least one applicant has a QUALIFIED or READY_TO_APPLY application."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT DISTINCT e.* FROM events e
               JOIN applications a ON e.event_id = a.event_id
               WHERE a.status IN ('QUALIFIED', 'READY_TO_APPLY')
               ORDER BY e.event_score DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_pending_applications(applicant_id: str) -> list[dict]:
    """Get pending applications for a specific applicant."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM applications WHERE applicant_id = ? AND status IN ('QUALIFIED', 'READY_TO_APPLY')",
            (applicant_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Run lock (file-based) ---

def acquire_run_lock() -> bool:
    """Try to acquire the daily run lock. Returns True if acquired."""
    lock_path = Path(settings.DATABASE_PATH).parent / "daily_run.lock"
    if lock_path.exists():
        # Check if stale (>2 hours old)
        mtime = lock_path.stat().st_mtime
        if time.time() - mtime > 7200:
            lock_path.unlink(missing_ok=True)
        else:
            return False
    lock_path.write_text(str(datetime.now(timezone.utc).isoformat()))
    return True


def release_run_lock() -> None:
    """Release the daily run lock."""
    lock_path = Path(settings.DATABASE_PATH).parent / "daily_run.lock"
    lock_path.unlink(missing_ok=True)
