# media-librarian

A stateful media-selection pipeline. It turns imperfect catalog observations
into normalized persistent state, verifies candidates before acting on them,
selects against a time-of-day taste profile, hands the pick to a launch
function, records what happened, and feeds outcomes back into later
selection. Python standard library only; `pytest` for the suite.

```
pip install -r requirements-dev.txt
python -m pytest             # ~10s; no network, browser, or credentials
```

Requires Python 3.10+ (CI runs 3.10 and 3.12) and SQLite 3.30+. The whole pipeline, including
ingestion, runs offline against fictional fixtures.

## The problem

In the original system a live TV channel was stored and treated as a film,
and a catalog harvest stopped after one slug while looking complete. Both
trace to the same cause: acting on an uncontrolled catalog goes wrong
quietly.

The catalog is outside the system's control: records arrive from several
sources with different completeness, items are mislabeled, pages disappear,
and networks fail. Acting on that directly produced real failures (below).
This pipeline separates what has been *observed* from what is *eligible* from
what has been *verified*, and makes each transition explicit and tested.

## Flow

```
fixtures/catalog.json        ← the source boundary (fictional, hand-authored)
        │   read through source.py / sitemap.py / verify.py via catalog.py
        ▼
registry_build               ingest: sitemap rows, category grids, verification   [writes registry]
        ▼
registry  (SQLite)           identity · merge rules · verification state · eligibility
        ▼
selection.resolve_window_candidates   registry rows per slug; grid fallback if a slug is thin
        ▼
selection.select_playable         ≤ 3 attempts · verify movies · resolve series to an episode
                             [writes verification state, episode_plays]
        ▼
serve.serve_once(launch)     one clock read → window → … → launch(url)
        ▼
record                       serve stamp · recently-played · events.jsonl   [writes all three]
        ▼
feedback.record_outcome ─►  feedback.adjustments ─► next selection   [writes feedback]
```

**Source boundary.** Everything the pipeline learns about the outside world
enters through three functions with live-style signatures: a category grid
(`source.search_category`), a show page (`source.fetch_movie_page`), and a
playability check (`verify.verify_playable`). Here they read
`fixtures/catalog.json`. No other code knows where data came from, and
`serve_once` takes the launch as a plain callable. Acquisition lives outside
this repository by design; the fixture makes everything downstream
deterministic and reproducible.

## One cycle, traced

Persistent state: the registry tables (`movies`, `episode_plays`,
`registry_meta`), the `feedback` table in the same database, `recently_played.json`
and `events.jsonl`.

This is the scenario in `tests/test_pipeline_e2e.py`, on the shipped fixture:

1. **Ingest.** The build CLI imports the sitemap rows, harvests the category
   grids and verifies. The registry holds 67 rows: 60 movies and 7 series.
   Live channels are never stored, and one item of an unrecognized type is
   skipped with a warning. Eight sitemap-only movies have no tags yet, so
   they are never candidates.
2. **Pick.** At `hour=21` the "Night" window sources `classics` from the
   registry (it has at least 20 rows) and `crime_tv` and `sci_fi` from the
   fixture grid (too few registry rows). The top-scoring title is `100207`
   at score 5.0 (tied with `100202`; the scorer's tie-breakers favor it). It
   verifies live, is launched (the test's launch function
   records the URL), and the serve is recorded in the registry, the
   recently-played list and `events.jsonl`.
3. **Control.** With only the recently-played list cleared, the same request
   picks `100207` again.
4. **Outcome.** `feedback.record_outcome(db, "100207", "skip")` counts a
   skip, worth −2.0.
5. **Next decision.** With the recently-played list cleared again, the same
   request now picks `100202` (5.0 against `100207`'s adjusted 3.0).

Step 3 is what makes step 5 mean something: the repeat-list alone would also
have changed the pick, so it is cleared in both runs and the difference is
the feedback's.

```
python -m pytest tests/test_pipeline_e2e.py -v
```

## What the registry guarantees

- **Identity** is `(content_type, video_id)`, permanent. A movie and a series
  that share an id are different records.
- **Merge rules are per field.** `first_seen_at` never changes; title, year
  and duration are first-good-value; tags and actors are replaced by each
  category snapshot; ratings take the latest observation.
- **Existing is not eligible, and eligible is not verified.** Eligibility is a
  per-type table (movies and series; a type not in the table is never
  returned). Verification is a separate state machine:
  `unknown → live | failed → unavailable`, with `transient_error` and
  `invalid_record` that do **not** count toward retirement. A title becomes
  `unavailable` only after 3 consecutive definitive failures.

## Verification and selection

Selection (`selection.select_playable`) calls verification, not the other way
round. It makes at most 3 attempts, withholds each rejected id from later
attempts in the same request, and treats the two kinds of title differently:
a movie whose check is inconclusive launches anyway, because its stored URL
plays as it is; a series is never launched until it resolves to a real
episode. If three attempts are spent, `serve_once` falls back to the best
movie rather than staying silent.

**Rejected ids are not recently-played ids.** Both keep a title out of a
pick, but they mean different things. Recently-played ids are a preference:
the scorer ignores them when honoring them would leave nothing to pick,
because repeating a title beats playing nothing. A rejected id is a fact
about this request: the title just failed verification or episode
resolution. Passed as exclusions, rejected ids would inherit the preference
rule, so once the pool ran short the scorer would return the same dead title
again, re-verify it, and count another definitive failure, enough to retire
it as `unavailable` on a single request. `select_playable` therefore removes
rejected titles from the candidate list itself, where that fallback cannot
reach them
(`test_selection.py::test_one_request_cannot_drive_a_title_to_unavailable`).

## Feedback

`feedback.record_outcome(db, video_id, "skip" | "positive")` counts outcomes
per title. The net becomes a clamped score adjustment that `serve_once` passes
into selection (a skip outweighs a positive). It is deliberately small: two
counters and one formula, not a recommender.

## Evidence

249 tests, run in about 10 seconds.

| What it shows | Where |
|---|---|
| The whole path: fixture → ingest → registry → serve → recorded outcome → feedback changes the next pick | `tests/test_pipeline_e2e.py` (14) |
| Ported end-to-end serve tests: fallbacks, series, transient failure, linear channels never launched | `tests/test_serve_once.py` (12) |
| Registry contract by operation | `tests/test_registry.py` (116) |
| Bounded selection, rejected-id withholding | `tests/test_selection.py` (24) |
| Partial harvests are loud | `tests/test_registry_build.py` (8) |
| Verification status policy | `tests/test_verify.py` (15) |
| Feedback | `tests/test_feedback.py` (11) |
| Episode ordering and sequencing | `tests/test_source.py` (18) |
| One-shot type migration | `tests/test_migrate_types.py` (13) |
| Scoring and dayparts; ingest warnings | `tests/test_taste_select_daypart.py` (10), `tests/test_ingest_warnings.py` (8) |

- **Offline.** The suite also passes with socket connect and DNS resolution
  patched to raise.
- **Manual mutation checks.** Done by hand during extraction, each making
  the expected tests fail: removing
  `hour=hour` from the first `select_playable` call in `serve.py` (3 tests
  fail), removing it from the fallback call (1 fails), and changing
  `feedback=adjust` to `feedback=None` at both call sites (3 fail). They are
  not automated, and they cover those three edits only.
- **The hour-boundary tests were corrected, not just ported.** Two tests in
  the original suite patched a function the code under test never calls, so
  they ran against the wrong profile and could not fail for the reason they
  described. In this repository a two-window profile is passed in and the
  clock is made to cross the hour during the request, so scoring, or the
  movie fallback, re-reading the clock now fails the tests (the first
  mutation above fails 3, including the fallback test). That makes them
  capable of failing for that regression. It does not prove hour handling in
  general.

## Failures that shaped it

Both cases are historical evidence from the original system, not fixture
output: the counts and durations below describe what happened there and are
not reproducible here. What is reproducible is the regression tests.

**Channels filed as movies.** Category grids carried live channels beside
films. Normalization routed only `series` to a series URL, so a channel fell
through to the movie branch and looked exactly like a movie. Verification
could not catch it, because a channel page really is live. A channel's
`duration: 0` was also stored as NULL, indistinguishable from a film with a
missing runtime. In the original system, 172 rows were affected before
anyone noticed. The fixes: classify by type, treat zero and missing durations
differently, make eligibility a per-type table, and migrate old rows with a
script that refuses on ambiguity (`bin/migrate_types.py`).

Regression tests: `test_registry.py::test_never_stores_a_linear_channel`,
`test_registry.py::test_linear_is_never_eligible`,
`test_serve_once.py::test_linear_from_the_grid_is_never_launched`, and
`test_migrate_types.py` (the migration refuses ambiguous rows).

**A half-finished harvest that looked complete.** In the original system the
category harvest raised `NameError` after its first slug on every run for six
days. The traceback was
visible, but nothing downstream treated a half-populated registry as different
from a full one, so selection quietly fell back to the grid. Now the harvest
continues past a failed slug, reports `attempted/succeeded/failed`, and exits
non-zero on a partial run.

Regression tests: `test_registry_build.py::TestPartialHarvestIsLoud`, including
`test_a_nameerror_is_caught_like_any_other_failure`.

## Provenance and boundaries

This is a snapshot extracted from a personal media-control system that
drove playback from an external video catalog. What was kept
unchanged or near it: the registry, verification policy, selection, series
sequencing, ingest accounting, migration. What is new here: `serve.py`
(extracted from the private trigger handler), `feedback.py`,
`recently_played.py`, `events.py`, `catalog.py` and the fixtures.

- **No acquisition code.** There are no fetchers, endpoints, request headers,
  tokens or page parsers for any live service. Nothing in this repository
  makes a network request.
- **Fixtures are fictional.** `fixtures/catalog.json` was authored for this
  repository; no title, id or metadata was captured from any service. URLs
  use a reserved `.test` host.
- **Service-specific names were removed.** Module, column and fixture names
  are generic; the URL and updated-at columns were renamed during extraction
  (`catalog_url`, `catalog_updated_at`).

## Known limitations

These exist in the original system and were deliberately not changed.

- **Series are not background-verified**; their state changes when served.
- **Availability filtering is partial.** A title outside its availability
  window is excluded only where a window has been stored.
- **A dead title seen only on a grid is not remembered.**
  `registry.record_verification` updates existing rows only.
- **Serve statistics are recorded but not read.** `serve_count` and
  `last_served_at` are stamped; repeat avoidance uses the bounded
  recently-played list, and `feedback.py` is the only path from outcomes back
  into selection.
- **One serve entry point.** `serve_once` is one of the original system's
  serve surfaces; the others are not included.

## Reproduce

```
cp taste_profile.example.json taste_profile.json
python bin/registry_build.py --import-sitemap
python bin/registry_build.py --enrich-categories
python bin/registry_build.py --verify
python bin/registry_build.py --stats
```

```python
# with bin/ on the path (PYTHONPATH=bin)
import json
from pathlib import Path
import serve, feedback

profile = json.load(open("taste_profile.example.json"))
serve.serve_once(profile, Path("registry.db"), launch=print,
                 recently_played_path=Path("recently_played.json"),
                 events_path=Path("events.jsonl"), hour=21)
feedback.record_outcome(Path("registry.db"), "100207", "skip")
```

(`registry_build.py` writes `registry.db` by default; pass `--db` to change it.)
Licensed under MIT.
