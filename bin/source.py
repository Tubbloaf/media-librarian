"""Catalog source: category grids, normalization, and episode resolution.

In the private system this module reads live catalog pages. In
this repository the fetch layer reads `fixtures/catalog.json` (see
catalog.py) and everything above it is unchanged: normalization of a raw
catalog entry into the pipeline's candidate dict, the content-type rules,
aggregated warnings, and series-to-episode resolution with sequencing.

Raw catalog entry (per title, "byId" shape): title, description, year,
duration (seconds), tags (genre strings), actors, directors, content_tags
{imdb_rating, rotten_tomatoes_score}, detailed_type.

detailed_type is one of "movie", "series" or "linear" (a live channel that
lives in the same grids and looks like a movie unless you check the type).
A fourth value, "episode", appears only on a show's own page.

Watch URLs: /movies/<id>/<slug> for a movie; /series/<id>/<slug> for a
series, with a leading zero stripped from the id. The series URL is a
landing page and is NOT playable -- resolve_playable_episode() turns a
picked series into /tv-shows/<episode_id>/<slug>. Call it on the final
pick only, never across a whole pool.

EPISODE ORDERING -- season identity exists only in the "S0X:EYY" prefix of
an episode's title. Structured season/episode numbers reset per season and
collide, so _parse_season_episode() parses the title. An episode whose
title has no such prefix cannot be placed in order and sorts after
everything that does (see resolve_playable_episode()).
"""
import re
from dataclasses import dataclass

import catalog
import ingest_warnings

def _slugify(title: str) -> str:
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


@dataclass
class EpisodeResolution:
    """Outcome of resolving a pick to something launchable.

    status is one of 'playable', 'not_found', 'no_episodes',
    'parse_error', 'transient_error'. url is set only when playable;
    episode_id/season/episode_number are set only when a series actually
    resolved to an episode whose title parsed (see EPISODE ORDERING in this
    module's docstring) -- season/episode_number stay None for a movie pass-
    through or a series with no parseable episode titles."""
    status: str
    url: str | None = None
    episode_id: str | None = None
    season: int | None = None
    episode_number: int | None = None
    failure_reason: str | None = None


_SEASON_EPISODE_RE = re.compile(r"^S(\d+):E(\d+)")


def _parse_season_episode(title: str) -> tuple[int, int] | None:
    """(season, episode) parsed from a title's "S0X:EYY" prefix, or None."""
    m = _SEASON_EPISODE_RE.match(title or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


class PageFetchError(Exception):
    """Raised by fetch_movie_page() on any fetch or parse failure."""
    def __init__(self, error_type: str, message: str = ""):
        self.error_type = error_type
        super().__init__(message or error_type)


_URL_ID_RE = re.compile(r"/(?:movies|series|tv-shows)/(\d+)/")


def fetch_movie_page(url: str, timeout: int = 10) -> dict:
    """Return the parsed page for a movie or show URL, or raise PageFetchError.

    PageFetchError.error_type values (the taxonomy callers rely on to tell a
    title that is gone from a network blip):
      'not_found'       -- the page does not exist
      'rate_limited'    -- caller should abort the run
      'server_error'    -- upstream 5xx
      'login_redirect'  -- the page sits behind a login wall
      'transient_error' -- network / timeout failure
      'parse_error'     -- page loaded but its data is unusable

    Here the page is synthesized from the fixture catalog: a show page lists
    its episodes as byId entries pointing back at the show via series_id.
    """
    m = _URL_ID_RE.search(url)
    content_id = catalog.find_id(m.group(1)) if m else None
    if content_id is None:
        raise PageFetchError("not_found", f"no such page: {url}")
    state = catalog.page_state(content_id)
    if state != "ok":
        raise PageFetchError(state, f"{state}: {url}")

    entry = catalog.entry(content_id)
    by_id = {content_id: dict(entry)}
    if entry.get("detailed_type") == "series":
        for ep in catalog.episodes(content_id):
            by_id[ep["episode_id"]] = {
                "detailed_type": "episode",
                "series_id": content_id.lstrip("0") or "0",
                "title": ep["title"],
            }
    return {"video": {"byId": by_id}}


def _fetch_category_page(slug: str, timeout: int = 10) -> dict:
    """Category grid in page-data shape; {} for an unknown slug, which is
    what a failed fetch looked like to callers of the live version."""
    children = catalog.category_children(slug)
    if not children:
        return {}
    return {
        "container": {"containerChildrenIdMap": {slug: children}},
        "video": {"byId": {cid: catalog.entry(cid) for cid in children}},
    }


def _normalize(video_id: str, v: dict, warns=None) -> dict:
    """Shape one byId entry for the rest of the pipeline.

    warns, when given, collects duration anomalies for the caller to emit
    in aggregate rather than one line per item."""
    detailed_type = v.get("detailed_type")
    slug = _slugify(v.get("title", ""))
    if detailed_type == "series":
        # confirmed live: series URLs use /series/ and strip a leading
        # zero from the id (see module docstring)
        url_id = video_id.lstrip("0") or "0"
        url = f"https://catalog.example.test/series/{url_id}/{slug}"
    else:
        url = f"https://catalog.example.test/movies/{video_id}/{slug}"

    # Absent and zero are different failures and must not be conflated.
    # the catalog sends duration 0 for a linear channel, and this used to be
    # passed through as falsy and stored as NULL, which is how 172 channels
    # ended up indistinguishable from movies missing their runtime. A
    # missing key instead means the grid did not carry the field at all.
    duration = v.get("duration")
    if warns is not None and detailed_type == "movie":
        if "duration" not in v:
            warns.add("movie with no duration field", video_id)
        elif duration is None:
            warns.add("movie with a null duration", video_id)
        elif duration <= 0:
            warns.add(f"movie with a non-positive duration ({duration})", video_id)

    tags = [t.lower() for t in (v.get("tags") or [])]
    content_tags = v.get("content_tags") or {}

    return {
        "video_id": video_id,
        "title": v.get("title", ""),
        "description": v.get("description", ""),
        "url": url,
        "year": v.get("year"),
        "duration_seconds": duration,
        "tags": tags,
        "categories": tags,  # the catalog's items carry no per-item category field
        "stars": [a.lower() for a in (v.get("actors") or [])],
        "directors": v.get("directors") or [],
        "imdb_rating": content_tags.get("imdb_rating"),
        "rotten_tomatoes_score": content_tags.get("rotten_tomatoes_score"),
        "detailed_type": detailed_type,
    }


def search_category(slug: str, timeout: int = 10) -> list[dict]:
    """Fetch one category's content grid (~50 items, whatever the catalog's own
    page renders -- no pagination attempted; that's plenty for picking one
    title that fits a daypart)."""
    data = _fetch_category_page(slug, timeout=timeout)
    container = data.get("container", {})
    children = container.get("containerChildrenIdMap", {}).get(slug) or []
    by_id = data.get("video", {}).get("byId", {})
    results = []
    warns = ingest_warnings.Warnings(f"category {slug!r}")
    for video_id in children:
        v = by_id.get(video_id)
        if v:
            results.append(_normalize(video_id, v, warns=warns))
    warns.flush()
    return results


def resolve_playable_episode(
    video: dict, last_played_episode_id: str | None = None, timeout: int = 10
) -> EpisodeResolution:
    """Resolve a series to a playable episode URL, reporting how it went.

    Movies pass straight through as 'playable' -- their stored URL already
    plays. A series' stored URL is the show landing page, which mounts no
    <video> element at all, so it must be resolved to an episode's
    /tv-shows/<episode_id>/<slug> URL before anything can launch it.

    Uses fetch_movie_page()'s error taxonomy so a caller can tell a title
    that is genuinely gone from a network blip -- a 404, an empty episode
    list, a malformed page and a dropped connection must not look
    identical. That distinction is the whole point of this function:

        playable         resolved; url is safe to launch
        not_found        show page is 404 or behind a login wall
        no_episodes      page parsed, but carries no episode for this series
        parse_error      page loaded, embedded data unusable
        transient_error  network, timeout, rate limit, or 5xx

    Callers should treat not_found/no_episodes as confirmed and worth
    remembering, and parse_error/transient_error as this-attempt-only.

    Which episode gets chosen (see EPISODE ORDERING in this module's
    docstring for why title-parsing is the only ordering signal available):
      - last_played_episode_id is None (never played, or the caller doesn't
        track it): the earliest episode by parsed (season, episode).
      - last_played_episode_id is set and found in the parsed, sorted list:
        the next one after it, wrapping to the earliest episode after the
        last one (deliberate: a finished series restarts rather than
        stalling).
      - last_played_episode_id is set but not found (e.g. that episode left
        the catalog): treated the same as None.
      - no episode on this show has a parseable title: falls back to
        whichever episode dict iteration happens to yield first, matching
        this function's behaviour before ordering existed -- unordered is
        still playable, and this case has not been observed live (7/7
        sampled shows parsed cleanly)."""
    if video.get("detailed_type") != "series":
        return EpisodeResolution("playable", url=video["url"])

    show_id = video["video_id"]
    show_url = video["url"]
    try:
        data = fetch_movie_page(show_url, timeout=timeout)
    except PageFetchError as e:
        if e.error_type in ("not_found", "login_redirect"):
            status = "not_found"
        elif e.error_type == "parse_error":
            status = "parse_error"
        else:
            # rate_limited, server_error, transient_error -- all say "ask
            # again later", none say anything about the title itself.
            status = "transient_error"
        return EpisodeResolution(status, failure_reason=e.error_type)

    by_id = data.get("video", {}).get("byId", {})
    show_id_stripped = show_id.lstrip("0") or "0"

    episodes = []
    for episode_id, ep in by_id.items():
        if ep.get("detailed_type") != "episode":
            continue
        series_id = str(ep.get("series_id", ""))
        if series_id.lstrip("0") != show_id_stripped:
            continue
        title = ep.get("title", "")
        episodes.append({
            "episode_id": episode_id,
            "slug": _slugify(title),
            "season_episode": _parse_season_episode(title),
        })

    if not episodes:
        return EpisodeResolution("no_episodes")

    ordered = [e for e in episodes if e["season_episode"] is not None]
    ordered.sort(key=lambda e: e["season_episode"])

    if not ordered:
        chosen = episodes[0]
        season = episode_number = None
    else:
        ids_in_order = [e["episode_id"] for e in ordered]
        if last_played_episode_id in ids_in_order:
            idx = ids_in_order.index(last_played_episode_id)
            chosen = ordered[(idx + 1) % len(ordered)]
        else:
            chosen = ordered[0]
        season, episode_number = chosen["season_episode"]

    return EpisodeResolution(
        "playable",
        url=f"https://catalog.example.test/tv-shows/{chosen['episode_id']}/{chosen['slug']}",
        episode_id=chosen["episode_id"],
        season=season,
        episode_number=episode_number,
    )
