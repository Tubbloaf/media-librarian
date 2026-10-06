#!/usr/bin/env python3
"""Build and maintain the SQLite registry.

Every source here (sitemap, category grids, movie pages, playability) is the
fixture catalog -- see catalog.py -- so every mode runs offline.

Modes:
  --import-sitemap          Register the catalog's sitemap rows. Safe to
                            rerun; existing rows are updated only on
                            sitemap_lastmod and NULL fields.

  --enrich-categories       Harvest the category grids named by
                            taste_profile.json's daypart windows and persist
                            tag/actor/rating data. Pass --all-categories to
                            harvest every slug in the catalog instead.

  --enrich-movie-pages      Read individual movie pages to populate
                            availability_ends, imdb_id, directors, etc.
                            Uses a tiered priority queue (see below).
                            --budget N caps requests per run (default 50).
                            --dry-run prints queue sizes without fetching.

  --verify                  Run the background verifier against the priority
                            queue (never-checked, failed, stale, unavailable).
                            --limit N caps how many records are checked per run.

  --stats                   Print registry summary and exit.

Options for pointing at other files:
  --db PATH       registry database (default: registry.db in the repo root)
  --catalog PATH  fixture catalog (default: fixtures/catalog.json)

Usage examples:
  python registry_build.py --import-sitemap
  python registry_build.py --enrich-categories
  python registry_build.py --enrich-categories --all-categories
  python registry_build.py --enrich-movie-pages --budget 200
  python registry_build.py --enrich-movie-pages --dry-run
  python registry_build.py --enrich-movie-pages --budget 100 --priority 1
  python registry_build.py --enrich-movie-pages --delay 2.0 4.0 --stale-days 60
  python registry_build.py --verify --limit 100
  python registry_build.py --stats
"""
import argparse
import random
import sys
import time
from collections import Counter
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BIN_DIR.parent

sys.path.insert(0, str(BIN_DIR))
import catalog
import taste_select
import registry
import source
import sitemap
import verify

DB_PATH = PROJECT_DIR / "registry.db"

# Politeness delays the live system needs between requests. With a fixture
# catalog there is nothing to be polite to, so they default to zero.
VERIFY_REQUEST_DELAY = 0
CATEGORY_REQUEST_DELAY = 0


def _slugs_from_profile() -> list[str]:
    """All category slugs referenced by any daypart window in the profile."""
    try:
        profile = taste_select.load_profile()
    except FileNotFoundError:
        print("taste_profile.json not found; skipping profile slugs")
        return []
    seen = set()
    slugs = []
    for window in profile.get("dayparts", []):
        for entry in window.get("category_slugs", []):
            slug = entry["slug"] if isinstance(entry, dict) else entry
            if slug not in seen:
                seen.add(slug)
                slugs.append(slug)
    return slugs


def _all_category_slugs() -> list[str]:
    """Every category slug in the catalog."""
    return catalog.categories()


def cmd_import_sitemap(args) -> None:
    registry.init(DB_PATH)
    is_bootstrap = not registry.is_baseline_complete(DB_PATH)

    movies, n_failed = sitemap.fetch_all_movies()
    if not movies:
        print("no movies returned from sitemap; aborting")
        return

    print(f"registering {len(movies)} movies (bootstrap={is_bootstrap})...")
    registry.register_from_sitemap(DB_PATH, movies, is_baseline=is_bootstrap)

    if is_bootstrap:
        if n_failed == 0:
            registry.mark_baseline_complete(DB_PATH)
            print("baseline complete — all sitemap files imported")
        else:
            print(
                f"warning: {n_failed} sitemap file(s) failed — "
                "baseline marker NOT written; rerun --import-sitemap to complete"
            )

    s = registry.stats(DB_PATH)
    print(f"done. registry total={s['total']} enriched={s['enriched']}")


def cmd_enrich_categories(args) -> int:
    """Harvest each daypart slug into the registry.

    Returns a non-zero exit code if any slug failed. A harvest that dies
    partway used to look exactly like one that finished: cmd_enrich_
    categories() raised NameError on an undefined delay constant after its
    very first slug, every run from 2026-08-14 to 2026-08-20, and the only
    signal was a traceback nobody was reading. The registry sat
    half-populated for six days and presented as complete -- crime_tv held
    a single row, so every serve quietly fell back to the live grid.

    So: keep going past a failed slug, count what happened, and say so
    loudly enough that a partial result cannot be mistaken for a whole one.
    """
    registry.init(DB_PATH)
    if args.all_categories:
        slugs = _all_category_slugs()
        print(f"scanning all {len(slugs)} category slugs")
    else:
        slugs = _slugs_from_profile()
        print(f"scanning {len(slugs)} slugs from taste_profile.json dayparts")

    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []

    for i, slug in enumerate(slugs, 1):
        print(f"  [{i}/{len(slugs)}] {slug}", end=" ", flush=True)
        try:
            items = source.search_category(slug)
            registry.observe_category(DB_PATH, items, slug)
        except Exception as e:
            # One bad slug must not cost the other 24. Catch broadly on
            # purpose: the failure this exists for was a NameError, not
            # anything network-shaped that a narrower except would list.
            print(f"→ FAILED: {type(e).__name__}: {e}")
            failed.append((slug, f"{type(e).__name__}: {e}"))
        else:
            kinds = Counter(m.get("detailed_type") for m in items)
            print("→ " + ", ".join(f"{n} {k}" for k, n in kinds.most_common()))
            succeeded.append(slug)
        if i < len(slugs):
            time.sleep(CATEGORY_REQUEST_DELAY)

    s = registry.stats(DB_PATH)
    print(f"\nattempted={len(slugs)} succeeded={len(succeeded)} "
          f"failed={len(failed)}")
    print(f"enriched={s['enriched']}/{s['total']}")

    if failed:
        print(f"\n{'!' * 60}")
        print(f"PARTIAL HARVEST -- {len(failed)} of {len(slugs)} slugs failed.")
        print("The registry is now inconsistent with the profile: slugs that")
        print("failed keep whatever they held before this run, and serving")
        print("will fall back to the live grid for any that fall below the")
        print("candidate floor. Re-run before trusting registry coverage.")
        for slug, reason in failed:
            print(f"  {slug}: {reason}")
        print("!" * 60)
        return 1
    return 0


def cmd_enrich_movie_pages(args) -> None:
    registry.init(DB_PATH)
    budget = args.budget or 50
    delay_min, delay_max = args.delay
    avg_per_req = 0.8 + (delay_min + delay_max) / 2  # observed fetch time + avg delay

    if args.dry_run:
        tier_stats = registry.movie_page_enrichment_stats(
            DB_PATH, stale_days=args.stale_days, recent_days=args.recent_days,
        )
        tier_labels = {
            1: "category-enriched, no page",
            2: f"stale page data (>{args.stale_days}d)",
            3: f"recent sitemap, untouched (<{args.recent_days}d)",
            4: "background backfill",
        }
        total = sum(tier_stats.values())
        print(
            f"individual-page enrichment — dry run "
            f"(budget={budget}, delay={delay_min}–{delay_max}s)"
        )
        remaining = budget
        for tier in sorted(tier_stats):
            count = tier_stats[tier]
            drain = min(remaining, count) if remaining > 0 else 0
            note = f"  ← would drain {drain}" if drain else ""
            remaining -= drain
            label = tier_labels.get(tier, f"tier {tier}")
            print(f"  Tier {tier}  {label:<40} {count:>7,}{note}")
        print(f"  {'':46}{'─' * 8}")
        print(f"  {'Total eligible':<46}{total:>7,}")
        would_fetch = min(budget, total)
        est_min = would_fetch * avg_per_req / 60
        print(
            f"\n  Would fetch: {would_fetch} | "
            f"~{avg_per_req:.1f}s/req (fetch + delay) → ~{est_min:.1f} min"
        )
        return

    rows = registry.pending_movie_page_enrichment(
        DB_PATH,
        budget=budget,
        stale_days=args.stale_days,
        recent_days=args.recent_days,
        priority=args.priority,
        retry_failed=args.retry_failed,
    )
    print(
        f"enriching {len(rows)} movie pages "
        f"(budget={budget}, delay={delay_min}–{delay_max}s)"
    )
    ok = fail = 0

    for i, row in enumerate(rows, 1):
        video_id = row["video_id"]
        url = row.get("catalog_url")
        tier = row.get("tier", "?")

        if not url:
            print(f"  [{i}] {video_id} [T{tier}]: no URL → not_found")
            registry.record_page_fetch_failure(DB_PATH, video_id, "not_found")
            fail += 1
            continue

        try:
            data = source.fetch_movie_page(url)
        except source.PageFetchError as e:
            if e.error_type == "rate_limited":
                # A rate-limit signal applies to the whole run, not one title:
                # abort immediately rather than spend the rest of the budget.
                registry.record_page_fetch_failure(DB_PATH, video_id, "rate_limited")
                print(
                    f"  [{i}] {video_id} [T{tier}]: 429 rate-limited — "
                    f"aborting run after {i} request(s)"
                )
                break
            print(f"  [{i}] {video_id} [T{tier}]: {e.error_type}")
            registry.record_page_fetch_failure(DB_PATH, video_id, e.error_type)
            fail += 1
        else:
            by_id = data.get("video", {}).get("byId", {})
            v = by_id.get(video_id)
            if not v:
                print(f"  [{i}] {video_id} [T{tier}]: id_not_in_response")
                registry.record_page_fetch_failure(
                    DB_PATH, video_id, "id_not_in_response"
                )
                fail += 1
            else:
                ratings = v.get("ratings") or []
                mpaa = ratings[0].get("value") if ratings else None
                registry.record_page_fetch_success(DB_PATH, video_id, {
                    "title":               v.get("title"),
                    "year":                v.get("year"),
                    "duration_seconds":    v.get("duration"),
                    "tags":                [t.lower() for t in (v.get("tags") or [])],
                    "actors":              v.get("actors") or [],
                    "directors":           v.get("directors") or [],
                    "imdb_id":             v.get("imdb_id"),
                    "availability_starts": v.get("availability_starts"),
                    "availability_ends":   v.get("availability_ends"),
                    "country":             v.get("country"),
                    "lang":                v.get("lang"),
                    "mpaa_rating":         mpaa,
                    "gracenote_id":        v.get("gracenote_id"),
                    "catalog_updated_at":     v.get("updated_at"),
                })
                avail = v.get("availability_ends", "none")
                print(f"  [{i}] {video_id} [T{tier}]: ok (avail_ends={avail})")
                ok += 1

        if i < len(rows):
            time.sleep(random.uniform(delay_min, delay_max))

    print(f"done. ok={ok} fail={fail}")


def cmd_verify(args) -> None:
    registry.init(DB_PATH)
    limit = args.limit or 100
    queue = registry.pending_verification(DB_PATH, limit=limit)
    print(f"verifying {len(queue)} records (limit={limit})")

    ok = 0
    failed = 0
    network_errors = 0

    for i, item in enumerate(queue, 1):
        video_id = item["video_id"]
        url = item.get("catalog_url") or ""
        prior_status = item.get("verification_status") or "never_checked"

        if not url:
            print(f"  [{i}] {video_id}: no URL → invalid_record")
            failed += 1
            registry.record_verification(
                DB_PATH, video_id, "invalid_record", "",
                failure_reason="no_url_stored",
            )
            continue

        result = verify.verify_playable(video_id, url)
        registry.record_verification(
            DB_PATH, video_id, result.status,
            result.final_url or url,
            failure_reason=result.failure_reason,
        )

        # Bonus: update availability if the verify pass found it
        if result.status == "live" and (result.availability_ends or result.availability_starts):
            registry.observe_movie_page(DB_PATH, video_id, {
                "availability_starts": result.availability_starts,
                "availability_ends":   result.availability_ends,
            })

        status_icon = {"live": "✓", "failed": "✗", "transient_error": "~", "invalid_record": "!"}.get(result.status, "?")
        reason = f" ({result.failure_reason})" if result.failure_reason else ""
        print(f"  [{i}] {video_id} [{prior_status}] → {result.status}{reason} {status_icon}")

        if result.status == "live":
            ok += 1
        elif result.status == "transient_error":
            network_errors += 1
        else:
            failed += 1

        if i < len(queue):
            time.sleep(VERIFY_REQUEST_DELAY)

    print(f"done. ok={ok} failed={failed} network_errors={network_errors}")


def cmd_stats(args) -> None:
    if not DB_PATH.exists():
        print("registry does not exist yet; run --import-sitemap first")
        return
    s = registry.stats(DB_PATH)
    print(f"Registry: {DB_PATH}")
    print(f"  total movies:     {s['total']:,}")
    print(f"  enriched (tags):  {s['enriched']:,}")
    print(f"  unenriched:       {s['unenriched']:,}")
    print(f"  verification:")
    for status, count in sorted(s.get("verification", {}).items()):
        print(f"    {status:<20} {count:,}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--import-sitemap",      action="store_true")
    parser.add_argument("--enrich-categories",   action="store_true")
    parser.add_argument("--all-categories",      action="store_true",
                        help="harvest every catalog slug, not just daypart slugs")
    parser.add_argument("--enrich-movie-pages",  action="store_true")
    parser.add_argument("--verify",              action="store_true")
    parser.add_argument("--stats",               action="store_true")
    parser.add_argument("--db", default=None, help="registry database path")
    parser.add_argument("--catalog", default=None, help="fixture catalog path")

    # --verify controls
    parser.add_argument("--limit", type=int, default=None,
                        help="max records to check per --verify run (default 100)")

    # --enrich-movie-pages controls
    parser.add_argument("--budget", type=int, default=None,
                        help="max pages to fetch per --enrich-movie-pages run (default 50)")
    parser.add_argument("--delay", nargs=2, type=float, metavar=("MIN", "MAX"),
                        default=[0.0, 0.0],
                        help="random delay range between requests in seconds (default 0 0)")
    parser.add_argument("--priority", type=int, choices=[1, 2, 3, 4], default=None,
                        help="drain only tier 1/2/3/4 (default: all in priority order)")
    parser.add_argument("--stale-days", type=int, default=90, dest="stale_days",
                        help="age threshold for tier-2 stale refresh (default 90)")
    parser.add_argument("--recent-days", type=int, default=7, dest="recent_days",
                        help="recency window for tier-3 new sitemap entries (default 7)")
    parser.add_argument("--retry-failed", action="store_true", dest="retry_failed",
                        help="include records still in backoff window")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="print queue sizes by tier without making any requests")

    args = parser.parse_args()

    global DB_PATH
    if args.db:
        DB_PATH = Path(args.db)
    if args.catalog:
        catalog.use(args.catalog)

    if args.stats:
        cmd_stats(args)
        return 0

    if not any([args.import_sitemap, args.enrich_categories,
                args.enrich_movie_pages, args.verify]):
        parser.print_help()
        return 1

    if args.import_sitemap:
        cmd_import_sitemap(args)
    if args.enrich_categories:
        rc = cmd_enrich_categories(args)
        if rc:
            return rc
    if args.enrich_movie_pages:
        cmd_enrich_movie_pages(args)
    if args.verify:
        cmd_verify(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
