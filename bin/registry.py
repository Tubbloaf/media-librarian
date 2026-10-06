"""SQLite registry of every movie the pipeline has ever touched.

IDENTITY
    (content_type, video_id) is the permanent identity of a record.
    content_type is 'movie', 'series' or 'linear' (see
    migrate_types.py, which classified the rows that predated the
    distinction). Only movies and series are ever eligible for a pick;
    linear channels are retained solely as a labelled sample of the type
    that once masqueraded as movies. video_id is the catalog's numeric string
    ID, as it appears in byId keys and watch URLs. Everything else is
    knowledge attached to that identity over time by three independent
    ingestion paths:

    SITEMAP       -- gives title, description, duration, thumbnail, sitemap
                    lastmod.
    CATEGORY PAGE -- gives tags, actors, year, imdb_rating,
                    rotten_tomatoes_score, which categories a title appears
                    in.
    MOVIE PAGE    -- gives imdb_id, availability_starts/ends, directors,
                    country, lang, mpaa_rating, gracenote_id.

    Rating signal (imdb_rating) comes exclusively from category pages;
    individual movie pages carry imdb_id but not the numeric rating.

VERIFICATION STATE
    last_verified_at      — when verify_playable() last ran on this row
    last_verified_url     — URL that was fetched (after any redirects)
    verification_status   — one of five values:
                              NULL / 'unknown'  — never verified
                              'live'            — last check confirmed playable
                              'unavailable'     — N consecutive definitive failures
                              'transient_error' — last check was a network/timeout
                                                  problem; does NOT count toward
                                                  the unavailable threshold
                              'invalid_record'  — data-quality problem (no URL,
                                                  login-redirect with bad data,
                                                  etc.); does NOT count toward
                                                  the unavailable threshold and is
                                                  excluded from candidates
                              'failed'          — at least one definitive failure
                                                  but below UNAVAILABLE_THRESHOLD
    failure_reason        — free-text tag for the last failure cause
    consecutive_failures  — count of sequential definitive failures; resets
                            to 0 on any 'live' result; NOT incremented by
                            'transient_error' or 'invalid_record'

    A record is marked 'unavailable' only after UNAVAILABLE_THRESHOLD
    consecutive confirmed failures. 'unavailable' records are retried
    periodically (see pending_verification()); they are excluded from
    candidates_for_pick() but not deleted.

MERGE RULES
    first_seen_at     never changed after insert
    last_seen_at      updated on every touch (any ingestion path)
    title/description/duration_seconds/thumbnail_url  first-good-value
    year              first-good-value
    catalog_url          first-good-value (updated by verify if redirected)
    tags/actors       replaced on category-page observation (complete snapshot)
    imdb_rating/rotten_tomatoes_score  replaced on category observation
    directors/country/lang/mpaa_rating replaced on movie-page observation
    imdb_id/gracenote_id               replaced on movie-page observation
    availability_starts/ends           replaced on movie-page observation
    source_categories accumulated (union); never shrinks

Public write operations:
    register_from_sitemap()   — bulk-register from sitemap XML parse
    observe_category()        — attach category-page metadata
    observe_movie_page()      — attach individual-page metadata
    record_verification()     — stamp a verification result
    record_serve()            — stamp a serve event

Read operations:
    candidates_for_pick()     — records shaped for taste_select.pick()
    pending_verification()    — priority queue for background verifier
    get()                     — fetch a single record
    stats()                   — summary counts
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ingest_warnings

# Every detailed_type seen on catalog category grids as of 2026-08-20. Only
# 'movie' is stored; the others are recognized so observe_category() can
# skip them quietly rather than warning about content it already knows is
# not a movie. A value outside this set -- including a missing one -- is
# reported, since that is how linear channels got in as movies.
_KNOWN_DETAILED_TYPES = frozenset({"movie", "series", "linear"})

UNAVAILABLE_THRESHOLD = 3

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS movies (
    content_type          TEXT NOT NULL DEFAULT 'movie',
    video_id              TEXT NOT NULL,

    title                 TEXT,
    description           TEXT,
    duration_seconds      INTEGER,
    year                  INTEGER,
    catalog_url              TEXT,
    thumbnail_url         TEXT,

    tags                  TEXT NOT NULL DEFAULT '[]',
    actors                TEXT NOT NULL DEFAULT '[]',
    directors             TEXT NOT NULL DEFAULT '[]',
    source_categories     TEXT NOT NULL DEFAULT '[]',

    imdb_rating           REAL,
    rotten_tomatoes_score REAL,
    imdb_id               TEXT,
    mpaa_rating           TEXT,

    availability_starts   TEXT,
    availability_ends     TEXT,
    country               TEXT,
    lang                  TEXT,
    gracenote_id          TEXT,

    first_seen_at         TEXT NOT NULL,
    last_seen_at          TEXT NOT NULL,
    sitemap_lastmod       TEXT,
    category_page_observed_at TEXT,
    movie_page_observed_at    TEXT,

    catalog_updated_at           TEXT,
    is_baseline_seed          INTEGER NOT NULL DEFAULT 0,

    last_verified_at      TEXT,
    last_verified_url     TEXT,
    verification_status   TEXT,
    failure_reason        TEXT,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,

    movie_page_fetch_error              TEXT,
    movie_page_last_attempted_at        TEXT,
    movie_page_consecutive_failures     INTEGER NOT NULL DEFAULT 0,
    movie_page_retry_after              TEXT,

    last_served_at        TEXT,
    serve_count           INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (content_type, video_id)
)
"""

_CREATE_META_TABLE = """
CREATE TABLE IF NOT EXISTS registry_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

# Last-played episode per series, for "resume with the next episode"
# sequencing. One row per series -- a play history was never asked for,
# only "what's next," so there is nothing to gain from an append-only log.
_CREATE_EPISODE_PLAYS_TABLE = """
CREATE TABLE IF NOT EXISTS episode_plays (
    series_id   TEXT PRIMARY KEY,
    episode_id  TEXT NOT NULL,
    season      INTEGER,
    episode     INTEGER,
    played_at   TEXT NOT NULL
)
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_movies_verification_status ON movies(verification_status)",
    "CREATE INDEX IF NOT EXISTS idx_movies_last_seen_at ON movies(last_seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_movies_tags ON movies(tags) WHERE tags != '[]'",
    "CREATE INDEX IF NOT EXISTS idx_movies_category_page_observed_at ON movies(category_page_observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_movies_movie_page_observed_at ON movies(movie_page_observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_movies_sitemap_lastmod ON movies(sitemap_lastmod)",
    "CREATE INDEX IF NOT EXISTS idx_movies_movie_page_retry_after ON movies(movie_page_retry_after)",
]

# Columns added after the initial schema; applied to existing DBs on init.
_MIGRATION_COLUMNS = [
    "ALTER TABLE movies ADD COLUMN catalog_updated_at TEXT",
    "ALTER TABLE movies ADD COLUMN movie_page_fetch_error TEXT",
    "ALTER TABLE movies ADD COLUMN movie_page_last_attempted_at TEXT",
    "ALTER TABLE movies ADD COLUMN movie_page_consecutive_failures INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE movies ADD COLUMN movie_page_retry_after TEXT",
]


def init(db_path: Path) -> None:
    """Create the movies table and indexes. Safe to call repeatedly."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_META_TABLE)
        conn.execute(_CREATE_EPISODE_PLAYS_TABLE)

        # One-time migration: when is_baseline_seed is first added to an
        # existing DB, all current records are marked as baseline seeds and
        # the registry_meta marker is written so future sitemap imports know
        # baseline is complete.
        existing = {
            r[1]
            for r in conn.execute("PRAGMA table_info(movies)").fetchall()
        }
        if "is_baseline_seed" not in existing:
            conn.execute(
                "ALTER TABLE movies "
                "ADD COLUMN is_baseline_seed INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                "UPDATE movies SET is_baseline_seed = 1 WHERE content_type = 'movie'"
            )
            conn.execute(
                "INSERT OR IGNORE INTO registry_meta (key, value) "
                "VALUES ('baseline_import_complete', ?)",
                (_now(),),
            )

        # DBs that received the is_baseline_seed migration before registry_meta
        # existed need the marker written retroactively.
        has_baseline = conn.execute(
            "SELECT 1 FROM movies WHERE is_baseline_seed = 1 LIMIT 1"
        ).fetchone() is not None
        has_marker = conn.execute(
            "SELECT 1 FROM registry_meta WHERE key = 'baseline_import_complete' LIMIT 1"
        ).fetchone() is not None
        if has_baseline and not has_marker:
            conn.execute(
                "INSERT OR IGNORE INTO registry_meta (key, value) "
                "VALUES ('baseline_import_complete', ?)",
                (_now(),),
            )

        for sql in _MIGRATION_COLUMNS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists

        for idx in _INDEXES:
            conn.execute(idx)
        conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_availability() -> str:
    """Current UTC in the catalog's availability format: '2026-09-01T03:59:00.000Z'.

    Not _now() -- that renders the offset as '+00:00', which does not sort
    against a 'Z'-suffixed value of the same second."""
    t = datetime.now(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _lower_list(values: list[str] | None) -> list[str]:
    """Deduplicated lowercase list, order-preserving."""
    if not values:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        lv = v.lower() if isinstance(v, str) else v
        if lv not in seen:
            seen.add(lv)
            out.append(lv)
    return out


def _merge_source_categories(existing_json: str, new_slug: str) -> str:
    cats = json.loads(existing_json or "[]")
    if new_slug not in cats:
        cats.append(new_slug)
    return json.dumps(cats)


def is_baseline_complete(db_path: Path) -> bool:
    """Return True if the baseline sitemap import has been marked complete."""
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT 1 FROM registry_meta WHERE key = 'baseline_import_complete' LIMIT 1"
        ).fetchone() is not None


def mark_baseline_complete(db_path: Path) -> None:
    """Persist the baseline-import-complete marker.

    Call this from the sitemap-import coordinator only after every sitemap
    file has been successfully fetched and registered. Idempotent."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO registry_meta (key, value) "
            "VALUES ('baseline_import_complete', ?)",
            (_now(),),
        )
        conn.commit()


def register_from_sitemap(
    db_path: Path,
    movies: list[dict],
    *,
    is_baseline: bool = False,
) -> None:
    """Bulk-register movies from sitemap XML.

    Each dict: video_id, title, description, duration_seconds, catalog_url,
    thumbnail_url, sitemap_lastmod. All optional except video_id.

    is_baseline=True marks new rows as baseline seeds (is_baseline_seed=1).
    Pass True during initial bootstrap; False (default) for later refreshes.
    The baseline marker itself is managed by the caller — see
    is_baseline_complete() and mark_baseline_complete().

    New rows: full insert. Existing rows: update sitemap_lastmod,
    last_seen_at, and any NULL basic-metadata fields (first-good-value).
    Never overwrites title/description/duration or is_baseline_seed already set.
    content_type is always 'movie' for this path."""
    if not movies:
        return
    now = _now()
    seed_flag = 1 if is_baseline else 0
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO movies
                (content_type, video_id, title, description, duration_seconds,
                 catalog_url, thumbnail_url, sitemap_lastmod,
                 first_seen_at, last_seen_at, is_baseline_seed)
            VALUES ('movie', :video_id, :title, :description, :duration_seconds,
                    :catalog_url, :thumbnail_url, :sitemap_lastmod, :now, :now,
                    :seed_flag)
            ON CONFLICT(content_type, video_id) DO UPDATE SET
                sitemap_lastmod  = excluded.sitemap_lastmod,
                last_seen_at     = excluded.last_seen_at,
                title            = COALESCE(movies.title,            excluded.title),
                description      = COALESCE(movies.description,      excluded.description),
                duration_seconds = COALESCE(movies.duration_seconds, excluded.duration_seconds),
                catalog_url         = COALESCE(movies.catalog_url,         excluded.catalog_url),
                thumbnail_url    = COALESCE(movies.thumbnail_url,    excluded.thumbnail_url)
            """,
            [
                {
                    "video_id":         m["video_id"],
                    "title":            m.get("title"),
                    "description":      m.get("description"),
                    "duration_seconds": m.get("duration_seconds"),
                    "catalog_url":         m.get("catalog_url"),
                    "thumbnail_url":    m.get("thumbnail_url"),
                    "sitemap_lastmod":  m.get("sitemap_lastmod"),
                    "seed_flag":        seed_flag,
                    "now":              now,
                }
                for m in movies
            ],
        )
        conn.commit()


# detailed_type as the catalog reports it -> content_type as the registry stores
# it. Types absent here are never stored: 'linear' is a live TV channel
# with no launchable URL of its own, and anything new the catalog introduces
# should be looked at before it lands in the pool.
_STORED_CONTENT_TYPES = {"movie": "movie", "series": "series"}


def observe_category(db_path: Path, movies: list[dict], slug: str) -> None:
    """Attach metadata seen on a category page for slug.

    Each dict: video_id, detailed_type, and optionally title, year,
    duration_seconds, tags, stars/actors, imdb_rating,
    rotten_tomatoes_score, url/catalog_url.

    Movies and series are both stored, keyed (content_type, video_id) --
    the same numeric id can in principle appear under either. A series
    carries no duration and its URL is the /series/ show page, neither of
    which is a defect: candidates_for_pick() applies per-type rules, so a
    series is never measured against the movie invariant.

    tags/actors replace whatever is stored (category page is a complete
    snapshot). imdb_rating/rotten_tomatoes_score replace (latest is best).
    title/year/duration_seconds are first-good-value.
    source_categories accumulates slug."""
    if not movies:
        return
    # Match the accepted types explicitly rather than defaulting an absent
    # value to 'movie' -- that default is how 172 linear channels were
    # stored as movies before this filter existed, and it would mask any
    # new type the catalog introduces the same way. Sampling 606 items across 14
    # category slugs on 2026-08-20 found exactly three values: 'movie'
    # (528, all with a runtime), 'series' (72, no runtime, routed to a
    # /series/ URL by _normalize) and 'linear' (6, no runtime, but given a
    # /movies/ URL and so otherwise indistinguishable from a movie here).
    warns = ingest_warnings.Warnings(f"category {slug!r}")
    for m in movies:
        dt = m.get("detailed_type")
        if dt not in _KNOWN_DETAILED_TYPES:
            warns.add(
                f"unrecognized detailed_type {dt!r}, skipped -- check whether "
                "these belong in the registry", m.get("video_id"))
    warns.flush()

    rows = [(m, _STORED_CONTENT_TYPES[m["detailed_type"]]) for m in movies
            if m.get("detailed_type") in _STORED_CONTENT_TYPES]
    if not rows:
        return
    now = _now()

    # Ensure rows exist, then read current state for merge.
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO movies
                (content_type, video_id, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?)
            """,
            [(ct, m["video_id"], now, now) for m, ct in rows],
        )
        conn.commit()

        # Key the merge on the full primary key. Selecting by video_id alone
        # would let a movie and a series sharing an id read each other's
        # stored values.
        conn.row_factory = sqlite3.Row
        placeholders = ",".join(["(?,?)"] * len(rows))
        params: list = []
        for m, ct in rows:
            params += [ct, m["video_id"]]
        current = {
            (r["content_type"], r["video_id"]): r
            for r in conn.execute(
                f"SELECT content_type, video_id, title, year, duration_seconds, "
                f"catalog_url, source_categories FROM movies "
                f"WHERE (content_type, video_id) IN ({placeholders})",
                params,
            ).fetchall()
        }

        updates = []
        for m, ct in rows:
            vid = m["video_id"]
            cur = current.get((ct, vid))
            updates.append({
                "content_type":           ct,
                "video_id":               vid,
                "title":                  (cur["title"] if cur and cur["title"] else m.get("title")),
                "year":                   (cur["year"] if cur and cur["year"] else m.get("year")),
                "duration_seconds":       (cur["duration_seconds"] if cur and cur["duration_seconds"] else m.get("duration_seconds")),
                "catalog_url":               (cur["catalog_url"] if cur and cur["catalog_url"] else m.get("url") or m.get("catalog_url")),
                "tags":                   json.dumps(_lower_list(m.get("tags", []))),
                "actors":                 json.dumps(_lower_list(m.get("stars") or m.get("actors") or [])),
                "imdb_rating":            m.get("imdb_rating"),
                "rotten_tomatoes_score":  m.get("rotten_tomatoes_score"),
                "source_categories":      _merge_source_categories(
                                              cur["source_categories"] if cur else "[]", slug
                                          ),
                "category_page_observed_at": now,
                "last_seen_at":           now,
            })

        conn.executemany(
            """
            UPDATE movies SET
                title                    = :title,
                year                     = :year,
                duration_seconds         = :duration_seconds,
                catalog_url                 = :catalog_url,
                tags                     = :tags,
                actors                   = :actors,
                imdb_rating              = :imdb_rating,
                rotten_tomatoes_score    = :rotten_tomatoes_score,
                source_categories        = :source_categories,
                category_page_observed_at = :category_page_observed_at,
                last_seen_at             = :last_seen_at
            WHERE content_type = :content_type AND video_id = :video_id
            """,
            updates,
        )
        conn.commit()


def observe_movie_page(db_path: Path, video_id: str, data: dict) -> None:
    """Attach metadata from a movie's individual page.

    data fields (all optional): title, year, duration_seconds, tags,
    actors, directors, imdb_id, availability_starts, availability_ends,
    country, lang, mpaa_rating, gracenote_id.

    All fields replace whatever is stored (individual page is authoritative
    for these). title/year/duration_seconds are first-good-value (shared
    with other sources; only fills in if still NULL)."""
    now = _now()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO movies
                (content_type, video_id, first_seen_at, last_seen_at)
            VALUES ('movie', ?, ?, ?)
            """,
            (video_id, now, now),
        )
        conn.commit()

        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT title, year, duration_seconds FROM movies "
            "WHERE content_type = 'movie' AND video_id = ?",
            (video_id,),
        ).fetchone()

        tags = data.get("tags")
        actors = data.get("actors")
        directors = data.get("directors")

        conn.execute(
            """
            UPDATE movies SET
                title            = COALESCE(title,            :title),
                year             = COALESCE(year,             :year),
                duration_seconds = COALESCE(duration_seconds, :duration_seconds),
                directors        = COALESCE(NULLIF(:directors,'[]'), directors),
                imdb_id          = COALESCE(:imdb_id,         imdb_id),
                availability_starts = :availability_starts,
                availability_ends   = :availability_ends,
                country          = COALESCE(:country, country),
                lang             = COALESCE(:lang,    lang),
                mpaa_rating      = COALESCE(:mpaa_rating, mpaa_rating),
                gracenote_id     = COALESCE(:gracenote_id, gracenote_id),
                catalog_updated_at  = COALESCE(:catalog_updated_at, catalog_updated_at),
                movie_page_observed_at = :now,
                last_seen_at           = :now
            WHERE content_type = 'movie' AND video_id = :video_id
            """,
            {
                "video_id":           video_id,
                "title":              data.get("title"),
                "year":               data.get("year"),
                "duration_seconds":   data.get("duration_seconds"),
                "directors":          json.dumps(directors) if directors else "[]",
                "imdb_id":            data.get("imdb_id"),
                "availability_starts": data.get("availability_starts"),
                "availability_ends":   data.get("availability_ends"),
                "country":            data.get("country"),
                "lang":               data.get("lang"),
                "mpaa_rating":        data.get("mpaa_rating"),
                "gracenote_id":       data.get("gracenote_id"),
                "catalog_updated_at":    data.get("catalog_updated_at"),
                "now":                now,
            },
        )
        conn.commit()


_PAGE_FETCH_BACKOFF = {
    # (base_hours, cap_hours)
    "transient_error":    (1,   168),   # 1h base, 7d cap
    "server_error":       (4,   720),   # 4h base, 30d cap
    "rate_limited":       (4,   720),
    "parse_error":        (24,  720),   # 24h base, 30d cap
    "id_not_in_response": (24,  720),
    "login_redirect":     (168, 720),   # 7d base, 30d cap
    "not_found":          (720, 2160),  # 30d base, 90d cap
}


def _compute_retry_after(error_type: str, consecutive_failures: int) -> str:
    """ISO8601 timestamp after which this record is eligible for re-queue.

    consecutive_failures is the NEW (post-increment) count. First failure
    gives count=1 → base delay. Second failure → 2× base, etc."""
    base, cap = _PAGE_FETCH_BACKOFF.get(error_type, (24, 720))
    hours = min(base * (2 ** (consecutive_failures - 1)), cap)
    dt = datetime.now(timezone.utc) + timedelta(hours=hours)
    return dt.isoformat(timespec="seconds")


def record_page_fetch_failure(
    db_path: Path,
    video_id: str,
    error_type: str,
    content_type: str = "movie",
) -> None:
    """Record a failed individual-page fetch attempt.

    Increments movie_page_consecutive_failures and sets movie_page_retry_after
    based on error_type and the new failure count. Does NOT touch
    movie_page_observed_at (the last successful observation is preserved)."""
    now = _now()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT movie_page_consecutive_failures FROM movies "
            "WHERE content_type = ? AND video_id = ?",
            (content_type, video_id),
        ).fetchone()
        if row is None:
            return
        new_failures = (row["movie_page_consecutive_failures"] or 0) + 1
        retry_after = _compute_retry_after(error_type, new_failures)
        conn.execute(
            """
            UPDATE movies SET
                movie_page_fetch_error          = ?,
                movie_page_last_attempted_at    = ?,
                movie_page_consecutive_failures = ?,
                movie_page_retry_after          = ?,
                last_seen_at                    = ?
            WHERE content_type = ? AND video_id = ?
            """,
            (error_type, now, new_failures, retry_after, now, content_type, video_id),
        )
        conn.commit()


def record_page_fetch_success(
    db_path: Path,
    video_id: str,
    data: dict,
    content_type: str = "movie",
) -> None:
    """Record a successful individual-page fetch: store metadata and clear error state.

    Separate from observe_movie_page() so that the verify bonus-update path
    (which calls observe_movie_page() with partial availability data) does not
    inadvertently reset page-fetch error tracking."""
    observe_movie_page(db_path, video_id, data)
    now = _now()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE movies SET
                movie_page_fetch_error          = NULL,
                movie_page_last_attempted_at    = ?,
                movie_page_consecutive_failures = 0,
                movie_page_retry_after          = NULL
            WHERE content_type = ? AND video_id = ?
            """,
            (now, content_type, video_id),
        )
        conn.commit()


def pending_movie_page_enrichment(
    db_path: Path,
    budget: int = 50,
    stale_days: int = 90,
    recent_days: int = 7,
    priority: int | None = None,
    retry_failed: bool = False,
) -> list[dict]:
    """Priority-ordered list of records needing individual-page enrichment.

    Tier 1: category-enriched, no individual page yet
    Tier 2: stale individual page data (older than stale_days)
    Tier 3: recently sitemap-discovered, untouched (within recent_days)
    Tier 4: background sitemap backfill (all remaining untouched)

    Records with movie_page_retry_after in the future are excluded unless
    retry_failed=True. Pass priority={1..4} to drain only one tier.

    Returns list of {video_id, catalog_url, tier}."""
    if not db_path.exists():
        return []

    now = _now()
    stale_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=stale_days)
    ).isoformat(timespec="seconds")
    recent_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=recent_days)
    ).isoformat(timespec="seconds")

    retry_cond = (
        ""
        if retry_failed
        else "AND (movie_page_retry_after IS NULL OR movie_page_retry_after < :now)"
    )
    tier_cond = f"AND tier = {int(priority)}" if priority else ""

    sql = f"""
        SELECT video_id, catalog_url, tier FROM (
            SELECT video_id, catalog_url, sitemap_lastmod,
                CASE
                    WHEN category_page_observed_at IS NOT NULL
                         AND movie_page_observed_at IS NULL THEN 1
                    WHEN movie_page_observed_at IS NOT NULL
                         AND movie_page_observed_at < :stale_cutoff THEN 2
                    WHEN movie_page_observed_at IS NULL
                         AND category_page_observed_at IS NULL
                         AND is_baseline_seed = 0
                         AND first_seen_at > :recent_cutoff THEN 3
                    WHEN movie_page_observed_at IS NULL
                         AND category_page_observed_at IS NULL THEN 4
                    ELSE NULL
                END AS tier
            FROM movies
            WHERE content_type = 'movie'
              {retry_cond}
        ) WHERE tier IS NOT NULL
          {tier_cond}
        ORDER BY tier, sitemap_lastmod DESC NULLS LAST
        LIMIT :budget
    """
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, {
            "now":           now,
            "stale_cutoff":  stale_cutoff,
            "recent_cutoff": recent_cutoff,
            "budget":        budget,
        }).fetchall()
    return [dict(r) for r in rows]


def movie_page_enrichment_stats(
    db_path: Path,
    stale_days: int = 90,
    recent_days: int = 7,
) -> dict[int, int]:
    """Return {tier: count} of records eligible for individual-page enrichment.

    Uses the same tier logic as pending_movie_page_enrichment but applies no
    budget limit. Records in backoff (retry_after in the future) are excluded."""
    if not db_path.exists():
        return {}

    now = _now()
    stale_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=stale_days)
    ).isoformat(timespec="seconds")
    recent_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=recent_days)
    ).isoformat(timespec="seconds")

    sql = """
        SELECT tier, COUNT(*) FROM (
            SELECT
                CASE
                    WHEN category_page_observed_at IS NOT NULL
                         AND movie_page_observed_at IS NULL THEN 1
                    WHEN movie_page_observed_at IS NOT NULL
                         AND movie_page_observed_at < :stale_cutoff THEN 2
                    WHEN movie_page_observed_at IS NULL
                         AND category_page_observed_at IS NULL
                         AND is_baseline_seed = 0
                         AND first_seen_at > :recent_cutoff THEN 3
                    WHEN movie_page_observed_at IS NULL
                         AND category_page_observed_at IS NULL THEN 4
                    ELSE NULL
                END AS tier
            FROM movies
            WHERE content_type = 'movie'
              AND (movie_page_retry_after IS NULL OR movie_page_retry_after < :now)
        ) WHERE tier IS NOT NULL
        GROUP BY tier
    """
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(sql, {
            "now":           now,
            "stale_cutoff":  stale_cutoff,
            "recent_cutoff": recent_cutoff,
        }).fetchall()
    return {tier: count for tier, count in rows}


def record_verification(
    db_path: Path,
    video_id: str,
    status: str,
    url: str,
    failure_reason: str | None = None,
    content_type: str = "movie",
) -> None:
    """Stamp a verification result onto a registry row.

    status must be one of: 'live', 'failed', 'transient_error', 'invalid_record'.

    'live'            → reset consecutive_failures to 0, clear failure_reason,
                        set verification_status = 'live'.
    'transient_error' → update timestamps and verification_status only; do NOT
                        increment consecutive_failures (network errors are
                        transient and do not count toward unavailable).
    'invalid_record'  → set verification_status = 'invalid_record', update
                        timestamps; do NOT increment consecutive_failures
                        (data-quality failures are not title-availability
                        evidence). The record is excluded from candidates.
    'failed'          → increment consecutive_failures; if it reaches
                        UNAVAILABLE_THRESHOLD, set status to 'unavailable',
                        otherwise 'failed'. Record failure_reason.

    Always updates last_verified_at and last_verified_url."""
    now = _now()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT verification_status, consecutive_failures FROM movies "
            "WHERE content_type = ? AND video_id = ?",
            (content_type, video_id),
        ).fetchone()

        if row is None:
            return

        if status == "live":
            conn.execute(
                """
                UPDATE movies SET
                    verification_status  = 'live',
                    failure_reason       = NULL,
                    consecutive_failures = 0,
                    last_verified_at     = ?,
                    last_verified_url    = ?
                WHERE content_type = ? AND video_id = ?
                """,
                (now, url, content_type, video_id),
            )
        elif status in ("transient_error", "invalid_record"):
            # Transient network errors and data-quality problems do not
            # count toward the unavailable threshold.
            conn.execute(
                """
                UPDATE movies SET
                    verification_status = ?,
                    failure_reason      = ?,
                    last_verified_at    = ?,
                    last_verified_url   = ?
                WHERE content_type = ? AND video_id = ?
                """,
                (status, failure_reason, now, url, content_type, video_id),
            )
        else:  # 'failed' — definitive failure
            new_failures = (row["consecutive_failures"] or 0) + 1
            new_status = "unavailable" if new_failures >= UNAVAILABLE_THRESHOLD else "failed"
            conn.execute(
                """
                UPDATE movies SET
                    verification_status  = ?,
                    failure_reason       = ?,
                    consecutive_failures = ?,
                    last_verified_at     = ?,
                    last_verified_url    = ?
                WHERE content_type = ? AND video_id = ?
                """,
                (new_status, failure_reason, new_failures, now, url,
                 content_type, video_id),
            )
        conn.commit()


def record_serve(
    db_path: Path,
    video_id: str,
    content_type: str = "movie",
) -> None:
    """Stamp a serve event. Inserts a minimal stub row if needed."""
    if not Path(db_path).exists():
        raise FileNotFoundError(f"registry not initialized: {db_path}")
    now = _now()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO movies
                (content_type, video_id, first_seen_at, last_seen_at,
                 last_served_at, serve_count)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(content_type, video_id) DO UPDATE SET
                last_served_at = excluded.last_served_at,
                last_seen_at   = excluded.last_seen_at,
                serve_count    = serve_count + 1
            """,
            (content_type, video_id, now, now, now),
        )
        conn.commit()


def get_last_played_episode(db_path: Path, series_id: str) -> dict | None:
    """Last episode recorded for series_id, or None if it's never been played."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT episode_id, season, episode, played_at "
            "FROM episode_plays WHERE series_id = ?",
            (series_id,),
        ).fetchone()
    return dict(row) if row else None


def record_episode_play(
    db_path: Path,
    series_id: str,
    episode_id: str,
    season: int | None,
    episode: int | None,
) -> None:
    """Stamp series_id's most recently played episode. One row per series."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO episode_plays (series_id, episode_id, season, episode, played_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(series_id) DO UPDATE SET
                episode_id = excluded.episode_id,
                season     = excluded.season,
                episode    = excluded.episode,
                played_at  = excluded.played_at
            """,
            (series_id, episode_id, season, episode, _now()),
        )
        conn.commit()


# Structural requirements for a record to be launchable, by content type.
# Expressed per type rather than as one universal rule: the old universal
# "must have a duration" test excluded linear channels only as a side
# effect of their missing runtime, which is not a statement about what a
# movie needs and broke the moment series -- which never have a runtime --
# became eligible. A type absent from this table is never eligible, so
# linear and anything new the catalog introduces are excluded by default rather
# than by a rule that happens to catch them.
_ELIGIBILITY_BY_TYPE = {
    # A movie's stored URL is directly playable, and it always has a real
    # runtime. > 0 rather than IS NOT NULL: the catalog reports duration 0 for a
    # linear channel, so a zero is a type error, not a short film.
    "movie":  "catalog_url LIKE '%/movies/%' AND duration_seconds > 0",
    # A series' stored URL is the show landing page, which does not play.
    # It has no runtime of its own, and resolving it to an episode happens
    # at serve time -- see source.resolve_playable_episode().
    "series": "catalog_url LIKE '%/series/%'",
}


def candidates_for_pick(
    db_path: Path,
    exclude_ids: set[str] | None = None,
    content_types: tuple[str, ...] = ("movie", "series"),
    category_slugs: list[str] | None = None,
    now: str | None = None,
) -> list[dict]:
    """Return all enriched, scoreable records shaped for taste_select.pick().

    Enriched means tags != '[]' -- the record has been seen on at least
    one category page and has genre data the scorer can use.

    Eligibility is per content type, from _ELIGIBILITY_BY_TYPE above. A
    type with no entry there is never returned, which is what keeps linear
    channels out: not their missing runtime, which was only ever a proxy,
    but the fact that nothing declares them launchable.

    category_slugs scopes the result to records seen on those category
    pages, matching source_categories -- the registry equivalent of the
    live path's per-slug search_category() fan-out. Omit it to draw from
    the whole enriched catalog.

    Records outside their availability window are excluded. the catalog stores
    these as fixed-width ISO8601 UTC ('2026-09-01T03:59:00.000Z'), which
    sorts lexicographically, so a string compare is exact. Note this only
    binds where the data exists: availability_ends is populated by
    movie-page enrichment and verification, and covers ~13% of the pool,
    so an expired title with no stored window still gets through. The
    filter is a floor, not a guarantee. now defaults to the current UTC
    time and exists for tests.

    Included statuses: NULL (unknown/never-checked), 'live', 'failed',
    'transient_error' (network was down, title may be fine).
    Excluded: 'unavailable' (confirmed dead) and 'invalid_record'
    (data-quality problem -- no URL, cannot be verified or launched).

    A series returned here is not yet known to be playable -- only that
    the catalog lists it. Confirming an episode exists costs a page fetch, so it
    happens once on the final pick rather than across the whole pool."""
    if not db_path.exists():
        return []

    eligible = [t for t in content_types if t in _ELIGIBILITY_BY_TYPE]
    if not eligible:
        return []

    type_clause = " OR ".join(
        f"(content_type = '{t}' AND {_ELIGIBILITY_BY_TYPE[t]})" for t in eligible)

    ex_clause = ""
    ex_params: list = []
    if exclude_ids:
        placeholders = ",".join("?" * len(exclude_ids))
        ex_clause = f"AND video_id NOT IN ({placeholders})"
        ex_params = list(exclude_ids)

    cat_clause = ""
    cat_params: list = []
    if category_slugs:
        # source_categories is a JSON array of slugs; match the quoted slug
        # so a prefix like "drama" can't match "drama_classics".
        ors = " OR ".join(["source_categories LIKE ?"] * len(category_slugs))
        cat_clause = f"AND ({ors})"
        cat_params = [f'%"{slug}"%' for slug in category_slugs]

    sql = f"""
        SELECT content_type, video_id, title, description, catalog_url, year,
               duration_seconds, tags, actors, directors,
               imdb_rating, rotten_tomatoes_score, verification_status
        FROM movies
        WHERE ({type_clause})
          AND tags != '[]'
          AND (verification_status IS NULL
               OR verification_status IN ('live', 'failed', 'transient_error'))
          {ex_clause}
          {cat_clause}
          AND (availability_ends   IS NULL OR availability_ends   > ?)
          AND (availability_starts IS NULL OR availability_starts <= ?)
    """
    now = now or _now_availability()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            sql, ex_params + cat_params + [now, now]).fetchall()
        # Every sitemap-sourced movie carries a runtime -- true of the
        # catalog as built, not enforced by the schema. If that changes,
        # real movies drop out of the pool; say so rather than shrinking
        # silently. Missing and non-positive are counted apart because they
        # mean different things: absent runtime versus a linear channel's
        # zero that was never reclassified.
        missing, nonpositive = conn.execute(
            "SELECT COUNT(*) FILTER (WHERE duration_seconds IS NULL), "
            "       COUNT(*) FILTER (WHERE duration_seconds <= 0) "
            "FROM movies WHERE content_type = 'movie' AND tags != '[]' "
            "AND sitemap_lastmod IS NOT NULL"
        ).fetchone()
    for count, what in ((missing, "no duration_seconds"),
                        (nonpositive, "a non-positive duration_seconds")):
        if count:
            print(
                f"warning: {count} sitemap-sourced movie rows have {what} and "
                "were excluded from the candidate pool; the duration "
                "invariant in candidates_for_pick() no longer holds",
                file=sys.stderr,
            )

    return [_candidate_from_row(row) for row in rows]


def recently_served(
    db_path: Path,
    limit: int = 20,
    content_type: str = "movie",
) -> list[str]:
    """Return video_ids of recently served titles, most recent first."""
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT video_id FROM movies WHERE content_type = ? "
            "AND last_served_at IS NOT NULL "
            "ORDER BY last_served_at DESC LIMIT ?",
            (content_type, limit),
        ).fetchall()
    return [r[0] for r in rows]


def pending_verification(
    db_path: Path,
    limit: int = 50,
    content_type: str = "movie",
) -> list[dict]:
    """Priority-ordered list of records needing verification.

    Priority:
      1. Unknown (status IS NULL) AND enriched (has tags) — never checked
      2. 'failed' or 'transient_error' — had issues, worth retrying
      3. 'live' but stale (last_verified_at older than 7 days)
      4. 'unavailable', for periodic retry (> 30 days since last check)

    'invalid_record' rows are NOT included — a missing URL or other
    data-quality failure is not something re-verification can fix.

    Returns list of {video_id, catalog_url, verification_status}."""
    if not db_path.exists():
        return []
    sql = """
        SELECT video_id, catalog_url, verification_status,
            CASE
                WHEN verification_status IS NULL AND tags != '[]' THEN 1
                WHEN verification_status IN ('failed', 'transient_error') THEN 2
                WHEN verification_status = 'live'
                 AND last_verified_at < datetime('now', '-7 days') THEN 3
                WHEN verification_status = 'unavailable'
                 AND (last_verified_at IS NULL
                      OR last_verified_at < datetime('now', '-30 days')) THEN 4
                ELSE NULL
            END AS priority
        FROM movies
        WHERE content_type = ?
          AND priority IS NOT NULL
        ORDER BY priority, last_verified_at ASC NULLS FIRST
        LIMIT ?
    """
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (content_type, limit)).fetchall()
    return [dict(r) for r in rows]


def get(
    db_path: Path,
    video_id: str,
    content_type: str = "movie",
) -> dict | None:
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM movies WHERE content_type = ? AND video_id = ?",
            (content_type, video_id),
        ).fetchone()
    return dict(row) if row else None


def stats(db_path: Path, content_type: str = "movie") -> dict:
    """Summary counts for monitoring / --stats output."""
    if not db_path.exists():
        return {}
    with sqlite3.connect(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM movies WHERE content_type = ?", (content_type,)
        ).fetchone()[0]
        enriched = conn.execute(
            "SELECT COUNT(*) FROM movies WHERE content_type = ? AND tags != '[]'",
            (content_type,),
        ).fetchone()[0]
        by_status = dict(
            conn.execute(
                "SELECT COALESCE(verification_status,'never_checked'), COUNT(*) "
                "FROM movies WHERE content_type = ? "
                "GROUP BY verification_status",
                (content_type,),
            ).fetchall()
        )
    return {
        "total": total,
        "enriched": enriched,
        "unenriched": total - enriched,
        "verification": by_status,
    }


def _candidate_from_row(row: sqlite3.Row) -> dict:
    """Shape a registry row for taste_select.pick()/score()."""
    tags = json.loads(row["tags"] or "[]")
    return {
        "video_id":            row["video_id"],
        "title":               row["title"],
        "description":         row["description"],
        "url":                 row["catalog_url"] or "",
        "year":                row["year"],
        "duration_seconds":    row["duration_seconds"],
        "tags":                tags,
        "categories":          tags,  # the catalog has no separate per-item category field
        "stars":               json.loads(row["actors"] or "[]"),
        "directors":           json.loads(row["directors"] or "[]"),
        "imdb_rating":         row["imdb_rating"],
        "rotten_tomatoes_score": row["rotten_tomatoes_score"],
        # taste_select and source both branch on this; it must be the
        # row's real type or a series will be launched as if it were a movie.
        "detailed_type":       row["content_type"],
        "_verification_status": row["verification_status"],
    }
