"""Shared candidate resolution for the serve surfaces.

Two callers (an unattended trigger and a hand-fired command) pick from the
same pool and must agree about what is launchable. They used to each carry
their own copy of the search-and-pick sequence, which is how one kept
fetching the live grid for six days after the other became registry-primary. This module holds the shared half; the surfaces keep
only what genuinely differs, which is what to do when nothing resolves.

Two things happen here:

  resolve_window_candidates()  registry-first per slug, live grid as a
                               per-slug fallback
  select_playable()            pick, confirm it will actually play, and
                               re-pick a bounded number of times if not

Series and movies fail differently, and the difference matters:

  A movie's stored URL is playable as it stands. If verification cannot
  reach it, the title is probably fine and the network is not, so it is
  launched anyway -- see item 6's policy.

  A series' stored URL is the show landing page, which mounts no video
  element at all. There is nothing to launch until an episode resolves,
  so an unresolved series is never launched hopefully. This asymmetry is
  deliberate. Do not "fix" it into consistency with the movie path.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN_DIR))

import taste_select
import registry
import source
import verify

# Minimum registry rows for a slug before falling back to the live grid.
# Roughly the size of the recently-played list, so a slug can never be
# exhausted by recent exclusions alone.
MIN_SLUG_CANDIDATES = 20

# Total candidate attempts, not re-picks: the first pick counts.
MAX_ATTEMPTS = 3

# Types the live grid may contribute. Linear channels are never launchable
# and are dropped here so the fallback cannot reintroduce what the registry
# already refuses to store.
_FALLBACK_TYPES = frozenset({"movie", "series"})


@dataclass
class Selection:
    """Outcome of selecting something to launch.

    status:
      'playable'  video and playable_url are set and safe to launch
      'no_match'  nothing in the pool cleared the taste profile
      'exhausted' every attempt resolved to something unplayable
    """
    status: str
    video: dict | None = None
    playable_url: str | None = None
    attempts: int = 0
    tried: list[str] = field(default_factory=list)


def resolve_window_candidates(window: dict, db_path: Path, log=print) -> list[dict]:
    """Candidates for a daypart window, registry-first with a live fallback.

    Each entry in window['category_slugs'] is a slug, or
    {'slug': ..., 'max_year': N} for the windows that want an era cap.
    """
    seen: dict[str, dict] = {}
    from_registry = 0
    from_api = 0

    for entry in window["category_slugs"]:
        if isinstance(entry, dict):
            slug = entry["slug"]
            max_year = entry.get("max_year")
        else:
            slug = entry
            max_year = None

        items = registry.candidates_for_pick(db_path, category_slugs=[slug])
        if len(items) >= MIN_SLUG_CANDIDATES:
            from_registry += 1
        else:
            # Below the floor the scorer has too little to choose between and
            # keeps landing on the same few titles. Measured 2026-08-20: 24 of
            # 25 configured slugs hold 22-61 rows. crime_tv is the exception --
            # the live grid returns it 50/50 series.
            if items:
                log(f"slug={slug!r} thin in registry ({len(items)} < "
                    f"{MIN_SLUG_CANDIDATES}), falling back to API")
            items = [
                m for m in source.search_category(slug)
                if m.get("detailed_type") in _FALLBACK_TYPES
            ]
            from_api += 1

        for item in items:
            if max_year is not None and item.get("year") and item["year"] > max_year:
                continue
            seen.setdefault(item["video_id"], item)

    log(f"candidates={len(seen)} "
        f"(registry slugs={from_registry}, api slugs={from_api})")
    return list(seen.values())


def _confirm_movie(video: dict, db_path: Path, log) -> tuple[str, str | None]:
    """Verify a movie and report ('launch'|'reject'|'skip', url)."""
    result = verify.verify_playable(video["video_id"], video["url"])
    registry.record_verification(
        db_path, video["video_id"], result.status,
        result.final_url or video["url"],
        failure_reason=result.failure_reason,
    )
    if result.status == "live":
        # verify_playable() also returns the availability window, and only
        # ~13% of the pool has one stored. Verifying exactly the titles that
        # get served populates that where it actually matters.
        if result.availability_ends or result.availability_starts:
            registry.observe_movie_page(db_path, video["video_id"], {
                "availability_starts": result.availability_starts,
                "availability_ends":   result.availability_ends,
            })
        return "launch", video["url"]

    if result.status == "transient_error":
        # The network is the problem, not the title, and the stored URL is
        # playable as it stands. record_verification() already declines to
        # count this as a failure.
        log(f"verify inconclusive for {video['title']!r} "
            f"({result.failure_reason}); launching anyway")
        return "launch", video["url"]

    return "reject", None


def _confirm_series(video: dict, db_path: Path, log) -> tuple[str, str | None]:
    """Resolve a series to an episode and report ('launch'|'reject'|'skip', url).

    Never returns 'launch' without a real episode URL. The show page does
    not play, so there is nothing to fall back to hopefully.

    Sequencing: looks up episode_plays for this series and resolves to the
    episode after whatever played last, wrapping to the earliest episode
    once the series is exhausted (a finished series restarts rather than
    stalling -- deliberate, decided 2026-08-20). First play for a series
    with no episode_plays row starts at its earliest episode. See
    source.resolve_playable_episode() for the ordering mechanics.
    """
    series_id = video["video_id"]
    last_played = registry.get_last_played_episode(db_path, series_id)
    last_played_episode_id = last_played["episode_id"] if last_played else None
    result = source.resolve_playable_episode(
        video, last_played_episode_id=last_played_episode_id)

    if result.status == "playable":
        registry.record_verification(
            db_path, video["video_id"], "live", result.url,
            content_type="series")
        registry.record_episode_play(
            db_path, series_id, result.episode_id, result.season,
            result.episode_number)
        return "launch", result.url

    if result.status in ("not_found", "no_episodes"):
        # Confirmed: this series has nothing to play. Worth remembering --
        # but record_verification() only updates an existing row, so this
        # sticks for registry-sourced series and is a no-op for one that
        # came from the live grid. crime_tv is entirely live-grid today, so
        # its dead series are re-tried on every serve until it becomes
        # registry-backed. Bounded attempts keep that cheap rather than
        # correct; it stops mattering once series ingestion lands.
        registry.record_verification(
            db_path, video["video_id"], "failed", video["url"],
            failure_reason=result.status, content_type="series")
        log(f"series {video['title']!r} is not playable ({result.status})")
        return "reject", None

    # parse_error / transient_error -- this attempt only, no permanent mark.
    log(f"series {video['title']!r} did not resolve ({result.status}); "
        "skipping for this request without penalising it")
    return "skip", None


def select_playable(
    candidates: list[dict],
    profile: dict,
    exclude_ids: set[str],
    db_path: Path,
    log=print,
    max_attempts: int = MAX_ATTEMPTS,
    hour: int | None = None,
    feedback: dict[str, float] | None = None,
) -> Selection:
    """Pick something that will actually play, within a bounded attempt count.

    Bounded because both callers are latency-sensitive: one runs on a
    gesture, the other on walking into a room. A dead slug must not walk
    the whole pool looking for a survivor.

    hour is the request's single daypart hour, from
    taste_select.current_hour(). Passing it keeps every re-pick scored
    under the same window that chose the candidates -- without it, pick()
    re-reads the clock on each attempt, and a request spanning an hour
    boundary would score later attempts under the next window.

    feedback is passed straight to taste_select.pick() (see feedback.py).
    """
    rejected: set[str] = set()
    tried: list[str] = []

    for attempt in range(1, max_attempts + 1):
        # Withhold rejected candidates from the list rather than passing them
        # in exclude_ids. taste_select.pick() deliberately ignores exclude_ids
        # when everything available is excluded -- the right call for
        # "everything has been played recently", but here it would hand back
        # the same dead title on every attempt, retest it, and drive it to
        # 'unavailable' on the strength of one request.
        remaining = [c for c in candidates if c["video_id"] not in rejected]
        if not remaining:
            return Selection("no_match", attempts=attempt - 1, tried=tried)

        video = taste_select.pick(
            remaining, profile, exclude_ids=exclude_ids, hour=hour,
            feedback=feedback)
        if video is None:
            return Selection("no_match", attempts=attempt - 1, tried=tried)

        tried.append(video["video_id"])
        if video.get("detailed_type") == "series":
            action, url = _confirm_series(video, db_path, log)
        else:
            action, url = _confirm_movie(video, db_path, log)

        if action == "launch":
            return Selection("playable", video=video, playable_url=url,
                             attempts=attempt, tried=tried)

        log(f"attempt {attempt}/{max_attempts}: {video['title']!r} unusable, "
            "re-picking")
        # Both 'reject' and 'skip' drop the candidate for this request; they
        # differ only in whether the registry remembers, which _confirm_*
        # has already decided.
        rejected.add(video["video_id"])

    return Selection("exhausted", attempts=max_attempts, tried=tried)
