"""Durable structured evidence for operators and conversational diagnosis."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Journal:
    """One writer per journal. Each completed line is independently readable."""

    def __init__(self, path: Path, *, run_id: str | None = None, stage: str | None = None):
        self.path = path
        self.run_id = run_id
        self.stage = stage

    def write(self, event: str, **details: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": 1,
            "time": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "stage": self.stage,
            "event": event,
            **details,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def tail_text(path: Path, *, limit: int = 32_768) -> str:
    """Bound reads even for multi-gigabyte training logs."""
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - limit))
            text = stream.read(limit).decode("utf-8", errors="replace")
        return ("[Earlier content omitted]\n" if size > limit else "") + text
    except OSError as exc:
        return f"[Evidence unavailable: {exc}]"


def redact(text: str, secrets: list[str]) -> str:
    for value in sorted(set(secrets), key=len, reverse=True):
        if value:
            text = text.replace(value, "[REDACTED]")
    return re.sub(
        r"(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        text,
    )


def secret_values(env: dict[str, str]) -> list[str]:
    return [
        value
        for key, value in env.items()
        if re.search(r"(?i)token|secret|password|credential|api.?key", key)
    ]
