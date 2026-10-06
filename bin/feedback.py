"""Outcome feedback: the smallest loop from "what happened" back to "what next".

After a serve, an outcome can be recorded against a title. Outcomes are
counted per title, and the net becomes a score adjustment the next
selection adds (taste_select.pick(feedback=...)).

    serve -> record_outcome -> feedback table -> adjustments() -> pick

Deliberately small. Two outcomes, two counters, one linear formula with a
clamp. The private system this was extracted from has a richer model for
a different catalog (per-daypart exposure, saturating positives, skip
weights, exploration); none of that is here, and this module should not be
read as a reproduction of it.

    adjustment = clamp(POSITIVE_WEIGHT * positive - SKIP_WEIGHT * skip,
                       -MAX_PENALTY, +MAX_BONUS)

A skip counts for more than a positive: rejection is stronger evidence than
approval, and the clamp keeps any one title from being buried or locked in
forever.

Identity matches the registry: (content_type, video_id).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

POSITIVE_WEIGHT = 1.0
SKIP_WEIGHT = 2.0
MAX_BONUS = 3.0
MAX_PENALTY = 6.0

OUTCOMES = ("positive", "skip")

_CREATE = """
CREATE TABLE IF NOT EXISTS feedback (
    content_type TEXT NOT NULL,
    video_id     TEXT NOT NULL,
    positive     INTEGER NOT NULL DEFAULT 0,
    skip         INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (content_type, video_id)
)
"""


def init(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(_CREATE)
        conn.commit()


def record_outcome(db_path: Path, video_id: str, outcome: str,
                   content_type: str = "movie") -> None:
    """Count one outcome ('positive' or 'skip') against a title."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}; expected one of {OUTCOMES}")
    init(db_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pos, skip = (1, 0) if outcome == "positive" else (0, 1)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO feedback (content_type, video_id, positive, skip, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(content_type, video_id) DO UPDATE SET
                positive   = positive + excluded.positive,
                skip       = skip + excluded.skip,
                updated_at = excluded.updated_at
            """,
            (content_type, video_id, pos, skip, now),
        )
        conn.commit()


def adjustment(positive: int, skip: int) -> float:
    raw = POSITIVE_WEIGHT * positive - SKIP_WEIGHT * skip
    return max(-MAX_PENALTY, min(MAX_BONUS, raw))


def adjustments(db_path: Path, candidates: list[dict]) -> dict[str, float]:
    """{video_id: adjustment} for the candidates that have feedback.

    Read path: tolerates a database that has never recorded feedback (no
    table yet) by returning {}, so selection never needs to initialize it."""
    if not candidates or not Path(db_path).exists():
        return {}
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT content_type, video_id, positive, skip FROM feedback"
            ).fetchall()
    except sqlite3.OperationalError:
        return {}
    stored = {(ct, vid): adjustment(p, s) for ct, vid, p, s in rows}
    out = {}
    for c in candidates:
        key = (c.get("detailed_type", "movie"), c["video_id"])
        if key in stored:
            out[c["video_id"]] = stored[key]
    return out
