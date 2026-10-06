"""Bounded list of recently served parent ids, persisted as JSON.

A pick is recorded here by its PARENT id -- a series' show id, never a
resolved episode id -- so one multi-episode series cannot occupy several
slots. taste_select.pick() treats these as an exclusion, and falls back to
ignoring the exclusion when it would leave nothing to pick.

A missing or unparseable file degrades to "remembers nothing" rather than
raising: this state exists to avoid repeats, and a repeat is better than a
crash.
"""
from __future__ import annotations

import json
from pathlib import Path

MAX_RECENT = 20


def load(path: Path) -> list[str]:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []
    ids = data.get("video_ids") if isinstance(data, dict) else data
    return [str(i) for i in ids] if isinstance(ids, list) else []


def save(path: Path, video_ids: list[str]) -> None:
    Path(path).write_text(json.dumps({"video_ids": list(video_ids)}))


def append(video_ids: list[str], video_id: str, limit: int = MAX_RECENT) -> list[str]:
    """Append in place and trim from the front to `limit`; returns the list."""
    video_ids.append(video_id)
    while len(video_ids) > limit:
        video_ids.pop(0)
    return video_ids
