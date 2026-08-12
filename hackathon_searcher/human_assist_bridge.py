"""Loopback-only bridge between prepared applications and the local Chrome extension."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from hackathon_searcher.database import get_application, get_event_by_id, log_audit

HOST = "127.0.0.1"
PORT = 8765
SELECTION_PATH = Path("human_assist") / "active_selection.json"
HANDSHAKE_PATH = Path("human_assist") / "extension_handshake.json"


def prepared_payload(event_id: str, applicant_id: str) -> dict:
    """Return only a tab-bound application enabled by the current team session."""
    if not SELECTION_PATH.exists():
        raise ValueError("No active human-assist application")
    selection = json.loads(SELECTION_PATH.read_text(encoding="utf-8"))
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
    }


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # Keep the detached bridge quiet.
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
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
        if urlparse(self.path).path != "/extension-handshake":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            # This endpoint stores only extension health metadata; it never
            # accepts profiles, answers, API keys, or arbitrary file paths.
            record = {
                "extension_version": str(payload.get("extension_version", ""))[:40],
                "luma_session": "present" if payload.get("luma_session") == "present" else "signed_out",
            }
            HANDSHAKE_PATH.parent.mkdir(parents=True, exist_ok=True)
            HANDSHAKE_PATH.write_text(json.dumps(record), encoding="utf-8")
            self._send(200, {"ok": True})
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": "invalid handshake"})
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
