"""
Database layer for Hackathon Searcher.

Uses SQLite for persistence. Stores events, applications, and audit logs.
All operations are idempotent where possible.
"""

import json
import sqlite3
import hashlib
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
    application_status TEXT DEFAULT '',
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
    travel_support TEXT DEFAULT 'UNKNOWN',
    travel_support_type TEXT DEFAULT '',
    travel_support_amount TEXT DEFAULT '',
    travel_support_currency TEXT DEFAULT '',
    travel_support_confidence REAL DEFAULT 0.0,
    travel_support_source TEXT DEFAULT '',
    travel_support_details TEXT DEFAULT '{}',
    flight_credits TEXT DEFAULT '',
    accommodation TEXT DEFAULT '',
    food TEXT DEFAULT '',
    score REAL DEFAULT 0.0,
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
    event_id TEXT NOT NULL,
    event_name TEXT NOT NULL,
    application_url TEXT DEFAULT '',
    questions TEXT DEFAULT '[]',
    answers TEXT DEFAULT '[]',
    date_applied TEXT DEFAULT '',
    application_confirmation TEXT DEFAULT '',
    application_reference TEXT DEFAULT '',
    submission_snapshot TEXT DEFAULT '{}',
    status TEXT DEFAULT 'READY_TO_APPLY',
    score REAL DEFAULT 0.0,
    score_reasoning TEXT DEFAULT '',
    travel_support_status TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    FOREIGN KEY (event_id) REFERENCES events(event_id),
    UNIQUE(event_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_id TEXT DEFAULT '',
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

CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
CREATE INDEX IF NOT EXISTS idx_events_score ON events(score);
CREATE INDEX IF NOT EXISTS idx_events_country ON events(country);
CREATE INDEX IF NOT EXISTS idx_events_start_date ON events(start_date);
CREATE INDEX IF NOT EXISTS idx_events_fingerprint ON events(fingerprint);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_applications_event_id ON applications(event_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_event_id ON audit_log(event_id);
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
    """Initialize the database schema."""
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# --- Event fingerprinting ---

def make_fingerprint(event_name: str, start_date: str, organizer: str = "", city: str = "") -> str:
    """Create a deterministic fingerprint to detect duplicate events."""
    normalized_name = _normalize(event_name)
    normalized_organizer = _normalize(organizer or "")
    normalized_city = _normalize(city or "")
    raw = f"{normalized_name}|{start_date}|{normalized_organizer}|{normalized_city}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _normalize(s: str) -> str:
    """Normalize a string for fingerprinting."""
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
    """Insert a new event. Returns the event_id."""
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
            "application_open", "application_status", "description", "themes",
            "sponsors", "judges", "partners", "prizes", "participant_limit",
            "age_requirement", "student_requirement", "nationality_requirement",
            "travel_support", "travel_support_type", "travel_support_amount",
            "travel_support_currency", "travel_support_confidence", "travel_support_source",
            "travel_support_details", "flight_credits", "accommodation", "food",
            "score", "score_reasoning", "confidence", "notes",
            "date_discovered", "date_last_checked", "status", "extra_data"
        ]

        values = [event.get(col, "") for col in columns]

        # Serialize list/dict fields to JSON
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
    """Update an existing event. Returns True if updated."""
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
                "SELECT * FROM events WHERE status = ? ORDER BY score DESC",
                (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM events ORDER BY score DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_events_needing_research() -> list[dict]:
    """Get events in DISCOVERED or RESEARCHING status."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM events WHERE status IN ('DISCOVERED', 'RESEARCHING') ORDER BY date_discovered ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_events_ready_to_apply() -> list[dict]:
    """Get qualified events ready for application."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM events WHERE status = 'QUALIFIED' ORDER BY score DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def has_application(event_id: str) -> bool:
    """Check if an application already exists for this event."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM applications WHERE event_id = ? AND status NOT IN ('SKIPPED', 'BLOCKED_CAPTCHA', 'BLOCKED_UNKNOWN_FIELD', 'BLOCKED_LOGIN')",
            (event_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def find_duplicate_events(event_name: str, start_date: str = "", city: str = "") -> list[dict]:
    """Find potential duplicate events using fuzzy matching."""
    conn = get_connection()
    try:
        normalized = _normalize(event_name)
        # Simple substring match on normalized names with same date
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


# --- Application CRUD ---

def insert_application(app: dict) -> int:
    conn = get_connection()
    try:
        list_fields = {"questions", "answers"}
        dict_fields = {"submission_snapshot"}
        for key, val in app.items():
            if key in list_fields and not isinstance(val, str):
                app[key] = json.dumps(val)
            elif key in dict_fields and not isinstance(val, str):
                app[key] = json.dumps(val)

        columns = [
            "event_id", "event_name", "application_url", "questions", "answers",
            "date_applied", "application_confirmation", "application_reference",
            "submission_snapshot", "status", "score", "score_reasoning",
            "travel_support_status", "notes"
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


def update_application(event_id: str, updates: dict) -> bool:
    conn = get_connection()
    try:
        list_fields = {"questions", "answers"}
        dict_fields = {"submission_snapshot"}
        for key, val in updates.items():
            if key in list_fields and not isinstance(val, str):
                updates[key] = json.dumps(val)
            elif key in dict_fields and not isinstance(val, str):
                updates[key] = json.dumps(val)

        set_clause = ", ".join([f"{k} = ?" for k in updates])
        values = list(updates.values()) + [event_id]
        conn.execute(f"UPDATE applications SET {set_clause} WHERE event_id = ?", values)
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def get_application(event_id: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM applications WHERE event_id = ?", (event_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_applications() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM applications ORDER BY date_applied DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Audit log ---

def log_audit(event_id: str, action: str, detail: str = "", level: str = "INFO") -> None:
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO audit_log (timestamp, event_id, action, detail, level) VALUES (?, ?, ?, ?, ?)",
            (now, event_id, action, detail, level)
        )
        conn.commit()
    finally:
        conn.close()


def get_audit_log(event_id: Optional[str] = None, limit: int = 100) -> list[dict]:
    conn = get_connection()
    try:
        if event_id:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE event_id = ? ORDER BY timestamp DESC LIMIT ?",
                (event_id, limit)
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
        row = conn.execute(
            "SELECT * FROM crawl_state ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
