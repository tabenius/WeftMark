"""Read Minotaur's optional host snapshot; never probe or control from a view.

Contract: ragbaz-minotaur/doc/runtime-status-v1.md. No optional runtime can
become a prerequisite for native evidence/review workflows.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

SCHEMA = "ragbaz.runtime-status.v1"
MAX_BYTES = 1024 * 1024


def load_runtime_status(path: str | None = None, *, now: datetime | None = None) -> dict:
    path = path or os.environ.get("RAGBAZ_RUNTIME_STATUS")
    missing = {"schema": SCHEMA, "status": "not-configured", "summary": [
        "Runtime status not configured (set RAGBAZ_RUNTIME_STATUS to Minotaur's snapshot).",
        "Native evidence and human review remain available without Ephor."]}
    if not path:
        return missing
    try:
        with Path(path).expanduser().open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("oversized snapshot")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise ValueError("unknown snapshot schema")
        timestamp = datetime.fromisoformat(data["observed_at"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None or not isinstance(data.get("components"), dict):
            raise ValueError("invalid observation")
        lines = data.get("summary")
        if not isinstance(lines, list) or len(lines) > 1024 or not all(isinstance(s, str) for s in lines):
            raise ValueError("invalid summary")
        age = ((now or datetime.now(timezone.utc)) - timestamp).total_seconds()
        status = "current" if 0 <= age <= 120 else "stale" if age > 120 else "clock-skew"
        # Terminal controls are never interpreted, including OSC hyperlinks.
        lines = ["".join(c if c.isprintable() else " " for c in s) for s in lines]
        return {**data, "status": status, "age_seconds": age,
                "summary": [f"Runtime snapshot: {status} (observations, not authority)"] + lines}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {**missing, "status": "unavailable", "summary": [
            "Runtime snapshot unavailable or invalid; optional services are unknown."]}


def runtime_text() -> str:
    return "\n".join(load_runtime_status()["summary"])
