"""Aggregated ingest warnings.

Ingest paths see the same anomaly many times in one batch -- a category
grid returning 50 linear channels used to print 50 near-identical lines.
Collect them by kind instead and emit one line each, carrying a count and
a few example ids so the warning stays actionable without drowning the
log it shares with everything else.

    warns = Warnings("category 'crime_tv'")
    for item in items:
        if bad(item):
            warns.add("unrecognized detailed_type 'sports'", item["video_id"])
    warns.flush()

Emits:

    warning: category 'crime_tv': unrecognized detailed_type 'sports'
    (3 items, e.g. 400000105, 670587, 684164)
"""
from __future__ import annotations

import sys

MAX_EXAMPLES = 3


class Warnings:
    """Collects warnings by kind, emits one aggregated line per kind."""

    def __init__(self, context: str, stream=None) -> None:
        self.context = context
        # dict preserves insertion order, so warnings come out in the order
        # they were first seen rather than sorted into something arbitrary.
        self._kinds: dict[str, list[str]] = {}
        self._counts: dict[str, int] = {}
        self._stream = stream

    def add(self, kind: str, example_id: str | None = None) -> None:
        self._counts[kind] = self._counts.get(kind, 0) + 1
        examples = self._kinds.setdefault(kind, [])
        if example_id is not None and len(examples) < MAX_EXAMPLES:
            examples.append(str(example_id))

    def flush(self) -> None:
        """Emit one line per kind, then reset. Silent when nothing was added."""
        stream = self._stream or sys.stderr
        for kind, examples in self._kinds.items():
            count = self._counts[kind]
            detail = f"{count} item{'s' if count != 1 else ''}"
            if examples:
                detail += ", e.g. " + ", ".join(examples)
            print(f"warning: {self.context}: {kind} ({detail})", file=stream)
        self._kinds.clear()
        self._counts.clear()

    def __bool__(self) -> bool:
        return bool(self._kinds)
