"""serve_once: one pick-and-launch cycle, end to end.

Extracted from the private system's presence-triggered handler. What it
kept is the orchestration, in order:

    one clock read
      -> daypart window
      -> candidates (registry first, live-grid fallback per thin slug)
      -> bounded, verified selection (movies verified, series resolved to an
         episode)
      -> movie fallback if nothing resolved
      -> recently-played, registry serve stamp
      -> launch(url)
      -> event record

What it dropped is everything about *how* a URL becomes playback: the
launch is a plain callable supplied by the caller. The tests pass one that
records the URL it was asked to launch; the private system passes one that
drives a browser on another device.

This surface is zero-touch: nobody is watching a log and there is no way to
report a failure, so when nothing resolves it falls through to the best
movie rather than staying silent. (The private system also has a
hand-fired surface that fails explicitly instead. That policy is not
reproduced here.)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import events
import feedback
import recently_played
import taste_select
import selection
import registry


@dataclass
class ServeResult:
    status: str            # 'launched' | 'no_window' | 'no_candidates'
                           # | 'no_match' | 'exhausted'
    video: dict | None = None
    url: str | None = None
    window: str | None = None
    attempts: int = 0
    used_movie_fallback: bool = False


def serve_once(
    profile: dict,
    db_path: Path,
    launch: Callable[[str], None],
    *,
    recently_played_path: Path,
    events_path: Path | None = None,
    log: Callable[[str], None] = print,
    hour: int | None = None,
) -> ServeResult:
    # One clock read for the whole request. Window selection, scoring and
    # the movie fallback all share it -- candidate resolution does I/O
    # between choosing the window and scoring what comes back, so an hour
    # boundary is genuinely reachable in between.
    if hour is None:
        hour = taste_select.current_hour()
    window = taste_select.get_current_daypart_window(profile, hour=hour)
    if window is None:
        log("no daypart window matches the current hour, giving up this cycle")
        events.record(events_path, "no_serve", reason="no_window", hour=hour)
        return ServeResult("no_window")
    name = window.get("_window")

    candidates = selection.resolve_window_candidates(window, db_path, log)
    if not candidates:
        log(f"window={name!r} -- no candidates found, giving up this cycle")
        events.record(events_path, "no_serve", reason="no_candidates",
                      window=name, hour=hour)
        return ServeResult("no_candidates", window=name)

    recent = recently_played.load(recently_played_path)
    exclude_ids = set(recent)
    adjust = feedback.adjustments(db_path, candidates)

    chosen = selection.select_playable(
        candidates, profile, exclude_ids, db_path, log, hour=hour,
        feedback=adjust)
    used_fallback = False

    if chosen.status == "exhausted":
        # Silence would read as a broken room, so fall through to the best
        # movie: a movie's stored URL is playable without resolution, which
        # is exactly what failed here.
        log(f"SERVE -- window={name!r} nothing resolved in "
            f"{chosen.attempts} attempts; falling back to a movie")
        movies = [c for c in candidates
                  if c.get("detailed_type") != "series"
                  and c["video_id"] not in chosen.tried]
        chosen = selection.select_playable(
            movies, profile, exclude_ids, db_path, log, hour=hour,
            feedback=adjust)
        used_fallback = True

    if chosen.status != "playable":
        log(f"window={name!r} -- {chosen.status} "
            f"({len(candidates)} candidates), giving up this cycle")
        events.record(events_path, "no_serve", reason=chosen.status,
                      window=name, hour=hour, attempts=chosen.attempts)
        return ServeResult(chosen.status, window=name,
                           attempts=chosen.attempts,
                           used_movie_fallback=used_fallback)

    video = chosen.video
    url = chosen.playable_url
    content_type = video.get("detailed_type", "movie")

    # The parent id, never a resolved episode id: one multi-episode series
    # must not consume several slots in the exclusion buffer.
    recently_played.append(recent, video["video_id"])
    recently_played.save(recently_played_path, recent)
    registry.record_serve(db_path, video["video_id"], content_type=content_type)

    score = taste_select.score(video, profile, hour=hour) \
        + adjust.get(video["video_id"], 0.0)
    log(f"SERVE -- window={name!r} picked: {video['title']!r} "
        f"({video.get('year')}) score={score:.1f} -> {url}")
    launch(url)
    events.record(
        events_path, "serve", video_id=video["video_id"],
        content_type=content_type, title=video["title"], url=url, window=name,
        hour=hour, attempts=chosen.attempts, score=round(score, 2),
        movie_fallback=used_fallback)
    return ServeResult("launched", video=video, url=url, window=name,
                       attempts=chosen.attempts,
                       used_movie_fallback=used_fallback)
