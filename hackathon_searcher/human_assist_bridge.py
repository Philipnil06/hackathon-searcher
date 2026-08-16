"""Loopback-only bridge between prepared applications and the local Chrome extension."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from hackathon_searcher.database import (
    get_application,
    get_applications_for_event,
    get_event_by_id,
    log_audit,
    update_application,
    update_event,
)
from hackathon_searcher.settings import settings

HOST = "127.0.0.1"
PORT = 8765
SELECTION_PATH = Path("human_assist") / "active_selection.json"
HANDSHAKE_PATH = Path("human_assist") / "extension_handshake.json"
SNAPSHOTS_DIR = Path("submission_snapshots")
REQUEST_LOG = Path("human_assist") / "bridge_requests.log"


def _log_request(path: str, payload: str) -> None:
    """Append a one-line trace of bridge activity for local debugging."""
    try:
        REQUEST_LOG.parent.mkdir(parents=True, exist_ok=True)
        with REQUEST_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat()} {path} {payload[:400]}\n")
    except OSError:
        pass


# Statuses that mean the applicant already has an irreversible submission on
# record. The bridge never downgrades or double-records those.
TERMINAL_SUBMITTED = {"APPLIED", "SUBMITTED", "MANUALLY_SUBMITTED"}


def _active_selection() -> dict:
    if not SELECTION_PATH.exists():
        raise ValueError("No active human-assist application")
    return json.loads(SELECTION_PATH.read_text(encoding="utf-8"))


def prepared_payload(event_id: str, applicant_id: str) -> dict:
    """Return only a tab-bound application enabled by the current team session."""
    selection = _active_selection()
    allowed_applicants = selection.get("applicant_ids") or [selection.get("applicant_id")]
    if selection.get("event_id") != event_id or applicant_id not in allowed_applicants:
        raise ValueError("Requested application is not the active selection")
    event = get_event_by_id(event_id)
    app = get_application(event_id, applicant_id)
    if not event or not app:
        raise ValueError("Prepared application not found")
    questions = json.loads(app.get("questions") or "[]")
    answers = json.loads(app.get("answers") or "[]")
    return {
        "event_id": event_id,
        "event_name": event.get("event_name", ""),
        "event_url": event.get("event_url", "") or app.get("application_url", ""),
        "applicant_id": applicant_id,
        "tab_applicant_binding": applicant_id,
        "applicant_name": app.get("applicant_name", ""),
        "email": next((str(item.get("answer", "")) for item in answers if "email" in str(item.get("label", "")).lower()), ""),
        "questions": questions,
        "answers": answers,
        "consent_selections": [item for item in answers if any(word in str(item.get("label", "")).lower() for word in ("consent", "agree", "terms", "privacy"))],
        "field_metadata": json.loads(app.get("form_metadata") or "{}"),
        # The extension may click Luma's final submit only when the operator
        # has explicitly enabled live auto-submit (DRY_RUN=false +
        # LUMA_AUTO_SUBMIT=true). Otherwise the user performs the irreversible
        # Luma click manually and the extension only records the outcome.
        "auto_submit": settings.should_auto_submit_luma,
        "confirmation_timeout_ms": settings.LUMA_CONFIRMATION_TIMEOUT_MS,
    }


def record_submission_result(event_id: str, applicant_id: str, result: dict) -> dict:
    """Record a submission outcome reported by the extension.

    The bridge is the authoritative recorder. It re-validates the live-mode
    gate and the active selection, rebuilds the snapshot from the stored
    application rather than trusting client-supplied answers, and writes the
    confirmation before marking the application APPLIED.
    """
    source = str(result.get("source", ""))
    if not settings.should_auto_submit_luma and source != "manual-click":
        raise ValueError("Submission recording requires a verified manual Luma click")
    selection = _active_selection()
    allowed_applicants = selection.get("applicant_ids") or [selection.get("applicant_id")]
    if selection.get("event_id") != event_id or applicant_id not in allowed_applicants:
        raise ValueError("Requested application is not the active selection")
    app = get_application(event_id, applicant_id)
    event = get_event_by_id(event_id)
    if not app or not event:
        raise ValueError("Prepared application not found")

    status = str(result.get("status", ""))
    if status not in {"APPLIED", "MANUALLY_SUBMITTED", "SUBMISSION_STATUS_UNKNOWN"}:
        raise ValueError("Invalid submission status")
    if status == "SUBMISSION_STATUS_UNKNOWN" and source != "manual-click" and not settings.should_auto_submit_luma:
        # In live auto-submit mode the extension reports the click before the
        # outcome is known so an unexpected navigation cannot lose the evidence.
        # Recording UNKNOWN is duplicate-safe: every downstream gate treats
        # UNKNOWN as already-attempted and never submits the same pair twice.
        raise ValueError("Unconfirmed submission cannot be recorded")

    existing = app.get("status", "")
    # Never downgrade a confirmed/irreversible submission to UNKNOWN.
    if existing in TERMINAL_SUBMITTED and status == "SUBMISSION_STATUS_UNKNOWN":
        return {"ok": True, "already_recorded": True, "status": existing}
    if existing == "APPLIED":
        return {"ok": True, "already_recorded": True, "status": existing}

    confirmation_text = str(result.get("confirmation_text", ""))[:2000]
    confirmation_url = str(result.get("confirmation_url", ""))[:1000]
    reference = str(result.get("confirmation_reference", ""))[:200]
    now = datetime.now(timezone.utc).isoformat()

    answers = json.loads(app.get("answers") or "[]")
    snapshot = {
        "event_name": event.get("event_name", ""),
        "event_id": event_id,
        "applicant": app.get("applicant_name", ""),
        "applicant_id": applicant_id,
        "event_score": app.get("event_score"),
        "applicant_fit_score": app.get("applicant_fit_score"),
        "travel_support_status": app.get("travel_support_status", ""),
        "application_url": app.get("application_url", ""),
        "form_provider": app.get("form_provider", ""),
        "email": next((str(item.get("answer", "")) for item in answers if "email" in str(item.get("label", "")).lower()), ""),
        "questions": json.loads(app.get("questions") or "[]"),
        "answers": answers,
        "status": status,
        "confirmation_text": confirmation_text,
        "confirmation_url": confirmation_url,
        "confirmation_reference": reference,
        "timestamp": now,
    }
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = re.sub(r"[^a-z0-9]+", "_", event.get("event_name", "event").lower()).strip("_")[:40]
    snapshot_path = SNAPSHOTS_DIR / f"{day}_{slug}_{applicant_id}.json"
    snapshot_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")

    update_application(event_id, applicant_id, {
        "status": status,
        "date_submitted": now,
        "application_confirmation": confirmation_text,
        "application_reference": reference,
        "submission_snapshot": json.dumps(snapshot),
    })
    apps = get_applications_for_event(event_id)
    if apps and all(a.get("status") in TERMINAL_SUBMITTED | {"SUBMISSION_STATUS_UNKNOWN"} for a in apps):
        update_event(event_id, {"team_status": "TEAM_APPLIED"})
    action = "LUMA_MANUAL_SUBMISSION_DETECTED" if source == "manual-click" else "LUMA_AUTO_SUBMITTED"
    log_audit(event_id, action, f"{applicant_id}: {status} — {confirmation_text[:160]}", applicant_id=applicant_id)
    return {"ok": True, "status": status, "snapshot": str(snapshot_path)}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # Keep the detached bridge quiet.
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        _log_request("GET " + parsed.path, "")
        if parsed.path == "/health":
            self._send(200, {"ok": True})
            return
        if parsed.path != "/prepared":
            self._send(404, {"error": "not found"})
            return
        query = parse_qs(parsed.query)
        try:
            self._send(200, prepared_payload(query.get("event_id", [""])[0], query.get("applicant_id", [""])[0]))
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(403, {"error": str(exc)})
            return

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, json.JSONDecodeError):
            payload = {}
        _log_request("POST " + path, json.dumps(payload, ensure_ascii=False))
        if path == "/extension-handshake":
            self._handle_handshake(payload)
            return
        if path == "/submission-result":
            self._handle_submission_result(payload)
            return
        self._send(404, {"error": "not found"})

    def _handle_handshake(self, payload: dict) -> None:
        # This endpoint stores only extension health metadata; it never
        # accepts profiles, answers, API keys, or arbitrary file paths.
        record = {
            "extension_version": str(payload.get("extension_version", ""))[:40],
            "luma_session": "present" if payload.get("luma_session") == "present" else "signed_out",
        }
        HANDSHAKE_PATH.parent.mkdir(parents=True, exist_ok=True)
        HANDSHAKE_PATH.write_text(json.dumps(record), encoding="utf-8")
        self._send(200, {"ok": True})

    def _handle_submission_result(self, payload: dict) -> None:
        try:
            result = record_submission_result(
                str(payload.get("event_id", "")),
                str(payload.get("applicant_id", "")),
                payload,
            )
            self._send(200, result)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(403, {"ok": False, "error": str(exc)})

    def _send(self, status: int, body: dict) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def run() -> None:
    ThreadingHTTPServer((HOST, PORT), _Handler).serve_forever()


if __name__ == "__main__":
    run()
