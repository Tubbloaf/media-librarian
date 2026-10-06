"""Append-only JSONL event log: what the pipeline did, in order.

One line per event: {"ts": ..., "kind": ..., **fields}. It is a record for a
human or a later script to read, not state the pipeline reads back -- the
state that feeds later decisions lives in the registry and in feedback.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def record(path: Path | None, kind: str, **fields) -> None:
    """Append one event. A None path disables recording."""
    if path is None:
        return
    line = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind, **fields}
    with open(path, "a") as f:
        f.write(json.dumps(line) + "\n")


def read(path: Path) -> list[dict]:
    try:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []
