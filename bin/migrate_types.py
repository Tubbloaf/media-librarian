#!/usr/bin/env python3
"""One-shot migration: classify registry rows by real content type.

Postmortem artifact. In the private system every row had been stored as
content_type='movie', because that is what both import paths assumed. Two
cohorts were not movies:

  series (45)   No URL at all. The sitemap only lists /movies/ URLs, so
                these arrived from category grids before observe_category()
                filtered on detailed_type, and normalization had no URL to
                build for them.

  linear (172)  A /movies/ URL and no duration. A live channel reports
                duration 0, which normalization passed through as falsy and
                observe_category() stored as NULL -- indistinguishable from
                a film whose runtime never arrived.

The expected counts (EXPECTED_SERIES, EXPECTED_LINEAR) are the private
database's. The migration checks them before touching anything and refuses
on a mismatch, so it will not run against the fixture-built registry in
this repository; its tests exercise it against synthetic databases.

The URL and updated-at columns had service-specific names in the original
database; they were renamed `catalog_url` and `catalog_updated_at` during
extraction. The SQL here uses the new names, so it would need the original
column names to run against the original database as it was.

Series rows also get their URL backfilled, using normalization's own rule
for series (strip the leading zero from the id, slugify the title).

content_type is half the primary key (content_type, video_id), so this
rewrites a key component. No 'series' or 'linear' rows existed, so no
collision was possible, but the migration asserts that rather than
assuming it.

Usage:
    migrate_types.py --dry-run     report what would change
    migrate_types.py --apply       perform the migration
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BIN_DIR.parent
DB_PATH = PROJECT_DIR / "registry.db"

sys.path.insert(0, str(BIN_DIR))
import source  # _slugify, for series URL reconstruction

EXPECTED_SERIES = 45
EXPECTED_LINEAR = 172

SERIES_WHERE = "content_type='movie' AND (catalog_url IS NULL OR catalog_url='')"
# Match /movies/ specifically rather than any http URL: a durationless row
# carrying a /series/ URL is neither cohort, and the coverage check below
# should catch it rather than quietly filing it under linear.
LINEAR_WHERE = (
    "content_type='movie' AND catalog_url LIKE '%/movies/%' "
    "AND duration_seconds IS NULL"
)


class MigrationError(RuntimeError):
    """Raised when a precondition or post-assertion fails."""


def _count(conn: sqlite3.Connection, where: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM movies WHERE {where}").fetchone()[0]


def series_url(video_id: str, title: str) -> str:
    """Rebuild a series URL the way source._normalize() does."""
    url_id = video_id.lstrip("0") or "0"
    return f"https://catalog.example.test/series/{url_id}/{source._slugify(title or '')}"


def check_preconditions(conn: sqlite3.Connection) -> tuple[int, int]:
    series = _count(conn, SERIES_WHERE)
    linear = _count(conn, LINEAR_WHERE)

    if series != EXPECTED_SERIES:
        raise MigrationError(
            f"expected {EXPECTED_SERIES} series rows, found {series}. The "
            "registry has changed since this migration was written; re-check "
            "the cohorts before running it.")
    if linear != EXPECTED_LINEAR:
        raise MigrationError(
            f"expected {EXPECTED_LINEAR} linear rows, found {linear}. The "
            "registry has changed since this migration was written; re-check "
            "the cohorts before running it.")

    # Every durationless movie must fall into exactly one cohort, or the
    # two rules above do not actually cover what they claim to.
    durationless = _count(conn, "content_type='movie' AND duration_seconds IS NULL")
    if durationless != series + linear:
        raise MigrationError(
            f"{durationless} durationless movies but the cohorts only account "
            f"for {series + linear}; some rows match neither rule.")

    for target in ("series", "linear"):
        existing = _count(conn, f"content_type='{target}'")
        if existing:
            raise MigrationError(
                f"{existing} rows are already content_type='{target}'; "
                "reclassifying could collide on the (content_type, video_id) "
                "primary key.")

    # A title is required to rebuild a series URL.
    untitled = _count(conn, f"{SERIES_WHERE} AND (title IS NULL OR title='')")
    if untitled:
        raise MigrationError(
            f"{untitled} series rows have no title, so no URL can be rebuilt "
            "for them.")

    return series, linear


def check_postconditions(conn: sqlite3.Connection, series: int, linear: int) -> None:
    problems = []

    bad = _count(conn, "content_type='movie' AND duration_seconds IS NULL")
    if bad:
        problems.append(f"{bad} movies still have no duration")

    bad = _count(conn, "content_type='series' AND catalog_url NOT LIKE '%/series/%'")
    if bad:
        problems.append(f"{bad} series rows lack a /series/ URL")

    got = _count(conn, "content_type='series'")
    if got != series:
        problems.append(f"expected {series} series rows, found {got}")

    got = _count(conn, "content_type='linear'")
    if got != linear:
        problems.append(f"expected {linear} linear rows, found {got}")

    bad = _count(conn, "content_type NOT IN ('movie','series','linear')")
    if bad:
        problems.append(f"{bad} rows carry an unrecognized content_type")

    if problems:
        raise MigrationError("post-migration assertions failed: " + "; ".join(problems))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()

    if not args.db.exists():
        print(f"no registry at {args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        series, linear = check_preconditions(conn)
    except MigrationError as e:
        print(f"aborting: {e}", file=sys.stderr)
        return 1

    print(f"registry: {args.db}")
    print(f"  -> series: {series}  (URL rebuilt from video_id + title)")
    print(f"  -> linear: {linear}  (kept for diagnostics, never eligible)")

    if args.dry_run:
        rows = conn.execute(
            f"SELECT video_id, title FROM movies WHERE {SERIES_WHERE} LIMIT 5"
        ).fetchall()
        print("\nsample series URLs that would be written:")
        for r in rows:
            print(f"  {r['video_id']:>10}  {series_url(r['video_id'], r['title'])}")
        conn.close()
        return 0

    to_series = conn.execute(
        f"SELECT video_id, title FROM movies WHERE {SERIES_WHERE}"
    ).fetchall()

    try:
        with conn:
            conn.executemany(
                "UPDATE movies SET content_type='series', catalog_url=? "
                "WHERE content_type='movie' AND video_id=?",
                [(series_url(r["video_id"], r["title"]), r["video_id"])
                 for r in to_series],
            )
            conn.execute(
                f"UPDATE movies SET content_type='linear' WHERE {LINEAR_WHERE}")
            check_postconditions(conn, series, linear)
    except MigrationError as e:
        # The context manager rolls back on the exception, so the registry is
        # left exactly as it was.
        print(f"rolled back: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print("\nmigration applied; all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
