"""Shared resumable cursor for historical backfills.

Each invocation scans one bounded date window. The cursor advances only after every
candidate in that window has either been successfully processed or rejected by the
model, and all webhook writes have been confirmed. The workflow commits the cursor
file and disables itself when the scan reaches today's end-exclusive date.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

STATE_DIR = Path(os.getenv("BACKFILL_STATE_DIR", ".backfill-state"))


def end_exclusive_today() -> str:
    """Return tomorrow's UTC date, so the full current UTC day is included."""
    return (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()


def _state_path(stream: str) -> Path:
    return STATE_DIR / f"{stream}.json"


def _read_state(stream: str) -> dict[str, Any]:
    path = _state_path(stream)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read backfill cursor {path}: {exc}") from exc


def _write_output(**values: str | bool) -> None:
    output_file = os.getenv("GITHUB_OUTPUT")
    if not output_file:
        return
    with open(output_file, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")


def choose_window(stream: str, date_from: str, date_to: str, window_days: int) -> tuple[str, str] | None:
    """Return the next date window or None if already caught up through today."""
    if window_days < 1:
        raise ValueError("BACKFILL_WINDOW_DAYS must be at least 1")
    state = _read_state(stream)
    cursor = str(state.get("next_start") or date_from)
    if cursor < date_from:
        cursor = date_from
    # Valid ISO dates sort lexicographically, but parse them to give a useful error.
    cursor_date = date.fromisoformat(cursor)
    stop_date = date.fromisoformat(date_to)
    if cursor_date >= stop_date:
        completed_through = (stop_date - timedelta(days=1)).isoformat()
        print(f"[backfill] {stream} already caught up through {completed_through}; no historical work needed.")
        _write_output(caught_up=True, window_advanced=False, next_start=cursor)
        return None
    next_end = min(cursor_date + timedelta(days=window_days), stop_date)
    return cursor_date.isoformat(), next_end.isoformat()


def advance_window(stream: str, window_start: str, window_end: str, date_to: str) -> bool:
    """Persist a completed window cursor. Returns True when caught up through today."""
    end_date = date.fromisoformat(window_end)
    stop_date = date.fromisoformat(date_to)
    caught_up = end_date >= stop_date
    payload = {
        "stream": stream,
        "next_start": window_end,
        "last_completed_window": {"start": window_start, "end_exclusive": window_end},
        "completed_through": (end_date - timedelta(days=1)).isoformat(),
        "status": "complete" if caught_up else "in_progress",
        "updated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path = _state_path(stream)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temp_path.replace(path)
    _write_output(caught_up=caught_up, window_advanced=True, next_start=window_end)
    print(f"[backfill] cursor advanced to {window_end}; completed through {payload['completed_through']}.")
    return caught_up


def leave_window_pending(window_start: str, window_end: str, reason: str) -> None:
    """Don't move the cursor when candidates or writes remain unresolved."""
    print(f"[backfill] Keeping cursor at {window_start}; window {window_start} to {window_end} is not complete: {reason}")
    _write_output(caught_up=False, window_advanced=False, next_start=window_start)
