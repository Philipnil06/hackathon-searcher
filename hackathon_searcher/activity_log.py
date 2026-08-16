"""Small, local activity journal for unattended runs."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LOG_DIR = Path("logs")
STATUS_PATH = LOG_DIR / "current_run.json"
MAX_LOG_BYTES = 2 * 1024 * 1024


def _json_safe(value: Any) -> Any:
    """Keep records small; logs must not include form answers or page text."""
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value[:20]]
    if isinstance(value, dict):
        return {str(key)[:80]: _json_safe(item) for key, item in list(value.items())[:30]}
    return str(value)[:500]


def log_activity(event: str, **details: Any) -> None:
    """Append a local timeline event and update the current run status."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = LOG_DIR / f"activity-{day}.jsonl"
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            archived = path.with_suffix(".previous.jsonl")
            if archived.exists():
                archived.unlink()
            path.replace(archived)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **{key: _json_safe(value) for key, value in details.items()},
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        STATUS_PATH.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


class StageTimer:
    """Record a named stage's wall-clock and CPU time."""

    def __init__(self, stage: str, **details: Any) -> None:
        self.stage = stage
        self.details = details
        self._wall_started = 0.0
        self._cpu_started = 0.0

    def __enter__(self) -> "StageTimer":
        self._wall_started = time.monotonic()
        self._cpu_started = time.process_time()
        log_activity("stage_started", stage=self.stage, **self.details)
        return self

    def finish(self, **details: Any) -> None:
        log_activity(
            "stage_completed", stage=self.stage,
            elapsed_seconds=round(time.monotonic() - self._wall_started, 2),
            cpu_seconds=round(time.process_time() - self._cpu_started, 2),
            **details,
        )

    def __exit__(self, exc_type, exc, _traceback) -> bool:
        if exc:
            log_activity("stage_failed", stage=self.stage,
                         elapsed_seconds=round(time.monotonic() - self._wall_started, 2),
                         error=str(exc))
        return False
