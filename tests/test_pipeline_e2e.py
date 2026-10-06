"""The whole pipeline, offline, from the shipped fixture catalog.

    fixture -> ingest (the build CLI) -> SQLite registry
            -> candidate resolution -> verified, bounded selection
            -> serve_once(launch) -> recording -> feedback -> next selection

Nothing here is mocked except the launch (a function that records the URL it
was asked to open) and the tie-break (pinned to the lowest id so a tie is
deterministic). The catalog, the registry, the verifier policy, series
resolution and the scorer are the real code.
"""
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import events
import feedback
import recently_played
import serve
import taste_select
import registry
import registry_build as build

PROFILE = json.loads((ROOT / "taste_profile.example.json").read_text())


@pytest.fixture(scope="module")
def built_db(tmp_path_factory):
    """Ingest the shipped catalog once, through the build CLI, and verify it."""
    db = tmp_path_factory.mktemp("registry") / "registry.db"
    mp = pytest.MonkeyPatch()
    mp.setattr(taste_select, "load_profile", lambda *a, **k: PROFILE)
    try:
        for flag in (["--import-sitemap"], ["--enrich-categories"],
                     ["--verify", "--limit", "500"]):
            mp.setattr(sys, "argv", ["build", "--db", str(db), *flag])
            assert build.main() == 0
    finally:
        mp.undo()
    return db


@pytest.fixture()
def pipe(built_db, tmp_path, monkeypatch):
    db = tmp_path / "registry.db"
    shutil.copy(built_db, db)
    # Ties between equally-scored titles are broken at random; pin that to
    # the lowest id so every scenario is deterministic.
    monkeypatch.setattr(taste_select.random, "choice",
                        lambda seq: min(seq, key=lambda v: v["video_id"]))

    class Pipe:
        launched: list = []
        logged: list = []
        recent_path = tmp_path / "recently_played.json"
        events_path = tmp_path / "events.jsonl"

        def __init__(self):
            self.db = db
            self.launched, self.logged = [], []

        def serve(self, hour, profile=PROFILE):
            return serve.serve_once(
                profile, self.db, self.launched.append,
                recently_played_path=self.recent_path,
                events_path=self.events_path, log=self.logged.append, hour=hour)

        def set_recent(self, ids):
            recently_played.save(self.recent_path, ids)

        def row(self, video_id, content_type="movie"):
            return registry.get(self.db, video_id, content_type)

    return Pipe()


def _url(video_id, slug):
    return f"https://catalog.example.test/movies/{video_id}/{slug}"


# --------------------------------------------------------------------------
# Ingest -> registry
# --------------------------------------------------------------------------

class TestIngest:
    def test_sitemap_rows_and_category_observations_merge_into_one_registry(
            self, built_db):
        import sqlite3
        with sqlite3.connect(built_db) as c:
            by_type = dict(c.execute(
                "SELECT content_type, COUNT(*) FROM movies GROUP BY 1").fetchall())
            unenriched = c.execute(
                "SELECT COUNT(*) FROM movies WHERE tags = '[]'").fetchone()[0]
        assert by_type["movie"] == 60      # every sitemap row; nothing duplicated
        assert by_type["series"] == 7      # only the category grids carry series
        assert "linear" not in by_type     # live channels never stored
        assert unenriched == 8             # sitemap-only titles: not candidates yet

    def test_unrecognized_types_are_skipped_not_stored(self, built_db):
        assert registry.get(built_db, "100599") is None

    def test_verification_states_follow_the_status_policy(self, built_db):
        assert registry.get(built_db, "100002")["verification_status"] == "live"
        failed = registry.get(built_db, "100003")
        assert (failed["verification_status"], failed["failure_reason"],
                failed["consecutive_failures"]) == ("failed", "http_404", 1)
        blip = registry.get(built_db, "100018")
        assert (blip["verification_status"], blip["consecutive_failures"]) \
            == ("transient_error", 0)

    def test_a_closed_availability_window_removes_a_title_from_the_pool(
            self, built_db):
        # 100020 verified live, but its declared window ended in 2020.
        ids = {c["video_id"] for c in registry.candidates_for_pick(built_db)}
        assert "100020" not in ids
        assert "100002" in ids


# --------------------------------------------------------------------------
# Candidate resolution, selection, verification
# --------------------------------------------------------------------------

class TestServe:
    def test_dense_slugs_come_from_the_registry_and_thin_ones_from_the_grid(
            self, pipe):
        # Night: crime_tv (7 rows) and sci_fi (10) are below the density
        # floor of 20 and fall back to the grid; classics (22) does not.
        r = pipe.serve(hour=21)
        assert r.status == "launched"
        assert any("registry slugs=1, api slugs=2" in line for line in pipe.logged)

    def test_a_movie_is_picked_launched_and_recorded(self, pipe):
        r = pipe.serve(hour=21)
        assert pipe.launched == [_url("100207", "afterglow-protocol")]
        assert r.video["video_id"] == "100207"
        assert recently_played.load(pipe.recent_path) == ["100207"]
        assert pipe.row("100207")["serve_count"] == 1
        (e,) = events.read(pipe.events_path)
        assert (e["kind"], e["video_id"], e["window"]) == ("serve", "100207", "Night")

    def test_an_unplayable_pick_is_rejected_recorded_and_not_retried(self, pipe):
        # Romance window ranks 100018, then 100003 (gone: 404), then 100012.
        # 100018 is set aside so the dead title is the first one tried.
        profile = {**PROFILE, "dayparts": [{
            "_window": "romance", "min_hour": 0, "max_hour": 24,
            "category_slugs": ["classics"], "tag_bonus": {"romance": 5}}]}
        before = pipe.row("100003")["consecutive_failures"]
        pipe.set_recent(["100018"])
        pipe.serve(hour=9, profile=profile)
        assert pipe.launched == [_url("100012", "rain-on-the-rialto")]
        gone = pipe.row("100003")
        assert gone["consecutive_failures"] == before + 1
        assert gone["failure_reason"] == "http_404"

    def test_a_transient_failure_launches_anyway_and_is_not_counted(self, pipe):
        profile = {**PROFILE, "dayparts": [{
            "_window": "romance", "min_hour": 0, "max_hour": 24,
            "category_slugs": ["classics"], "tag_bonus": {"romance": 5}}]}
        pipe.serve(hour=9, profile=profile)        # 100018 ranks first
        assert pipe.launched == [_url("100018", "fortune-street")]
        row = pipe.row("100018")
        assert (row["verification_status"], row["consecutive_failures"]) \
            == ("transient_error", 0)

    def test_series_resolve_to_an_episode_and_advance_in_order(self, pipe):
        pipe.serve(hour=12)       # Midday: three series tie; best-rated wins
        pipe.set_recent([])
        pipe.serve(hour=12)
        assert pipe.launched == [
            "https://catalog.example.test/tv-shows/5000101/s01-e01-pilot",
            "https://catalog.example.test/tv-shows/5000102/s01-e02-wrong-number",
        ]
        assert pipe.row("0400021100", "series")["verification_status"] == "live"

    def test_dead_series_fall_through_to_a_movie_and_are_remembered(self, pipe):
        # Dry Creek lists no episodes, Cold Harbor's page is gone, and the
        # third candidate (100102) fails verification: three attempts spent,
        # so the zero-touch fallback substitutes the best remaining movie.
        pipe.set_recent(["0400021100", "0400021101", "100103", "100101"])
        r = pipe.serve(hour=12)
        assert r.used_movie_fallback
        assert any("falling back to a movie" in line for line in pipe.logged)
        assert pipe.launched == [_url("100403", "nine-winters-in-ostrava")]
        assert pipe.row("100102")["failure_reason"] == "http_error"
        dry = pipe.row("0400021102", "series")
        assert (dry["verification_status"], dry["failure_reason"]) \
            == ("failed", "no_episodes")
        assert pipe.row("0400021103", "series")["failure_reason"] == "not_found"

    def test_nothing_launches_when_no_window_matches(self, pipe):
        profile = {**PROFILE, "dayparts": [PROFILE["dayparts"][0]]}   # 6-10 only
        r = pipe.serve(hour=15, profile=profile)
        assert (r.status, pipe.launched) == ("no_window", [])
        assert events.read(pipe.events_path)[0]["reason"] == "no_window"


# --------------------------------------------------------------------------
# The loop: outcome -> feedback -> later selection
# --------------------------------------------------------------------------

class TestFeedbackLoop:
    def test_a_skip_changes_what_is_picked_next(self, pipe):
        first = pipe.serve(hour=21)
        assert first.video["video_id"] == "100207"

        # Control: with only the repeat-exclusion cleared and no feedback,
        # the same title wins again. That is what the next step has to beat.
        pipe.set_recent([])
        assert pipe.serve(hour=21).video["video_id"] == "100207"

        feedback.record_outcome(pipe.db, "100207", "skip")
        pipe.set_recent([])
        assert pipe.serve(hour=21).video["video_id"] == "100202"

    def test_positive_outcomes_can_promote_a_lower_ranked_title(self, pipe):
        for _ in range(3):
            feedback.record_outcome(pipe.db, "100103", "positive")
        r = pipe.serve(hour=21)
        assert r.video["video_id"] == "100103"      # 4.0 + 3 beats 5.0

    def test_feedback_is_scoped_to_the_title_it_was_recorded_against(self, pipe):
        feedback.record_outcome(pipe.db, "100207", "skip")
        pipe.serve(hour=21)
        pipe.set_recent([])
        # Everything else is unaffected: the other 5.0 title keeps winning.
        assert pipe.serve(hour=21).video["video_id"] == "100202"
