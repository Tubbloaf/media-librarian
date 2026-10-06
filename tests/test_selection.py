"""Tests for selection.py -- shared candidate resolution and selection."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import selection
import registry
import source


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "registry_test.db"
    registry.init(p)
    return p


PROFILE = {"preferred_tags": {"thriller": 5}}


def _movie(video_id="m1", **kw):
    base = {
        "video_id": video_id, "title": f"Movie {video_id}", "year": 2000,
        "duration_seconds": 5400, "tags": ["thriller"], "categories": ["thriller"],
        "stars": [], "directors": [], "imdb_rating": None,
        "rotten_tomatoes_score": None, "detailed_type": "movie",
        "url": f"https://catalog.example.test/movies/{video_id}/m",
    }
    return {**base, **kw}


def _series(video_id="s1", **kw):
    return _movie(video_id, detailed_type="series",
                  url=f"https://catalog.example.test/series/{video_id}/s", **kw)


class _Log:
    def __init__(self):
        self.lines = []

    def __call__(self, line):
        self.lines.append(line)

    def text(self):
        return "\n".join(self.lines)


@pytest.fixture()
def log():
    return _Log()


def _stub_verify(monkeypatch, status, reason=None):
    import verify
    monkeypatch.setattr(verify, "verify_playable",
                        lambda *a, **k: verify.VerifyResult(
                            status=status, failure_reason=reason))


def _stub_resolve(monkeypatch, results):
    """results: list of EpisodeResolution, consumed in order."""
    calls = iter(results)
    monkeypatch.setattr(source, "resolve_playable_episode",
                        lambda *a, **k: next(calls))


class TestSelectPlayableMovies:
    def test_live_movie_is_launched(self, db, log, monkeypatch):
        _stub_verify(monkeypatch, "live")
        sel = selection.select_playable([_movie()], PROFILE, set(), db, log)
        assert sel.status == "playable"
        assert sel.playable_url == "https://catalog.example.test/movies/m1/m"

    def test_transient_movie_launches_anyway(self, db, log, monkeypatch):
        # A movie's stored URL plays as it stands; the network is the problem.
        _stub_verify(monkeypatch, "transient_error", "transient_error")
        sel = selection.select_playable([_movie()], PROFILE, set(), db, log)
        assert sel.status == "playable"

    def test_dead_movie_is_rejected_and_repicked(self, db, log, monkeypatch):
        # Only m2 is alive. Tie-breaks are random, so assert the outcome
        # rather than the order the two were tried in.
        import verify
        seen = []

        def fake(video_id, url, **k):
            seen.append(video_id)
            status = "live" if video_id == "m2" else "failed"
            return verify.VerifyResult(status=status,
                                            failure_reason="http_404")
        monkeypatch.setattr(verify, "verify_playable", fake)
        sel = selection.select_playable(
            [_movie("m1"), _movie("m2")], PROFILE, set(), db, log)
        assert sel.status == "playable"
        assert sel.video["video_id"] == "m2"
        assert "m2" in seen and len(seen) == len(set(seen))


class TestSelectPlayableSeries:
    def test_resolved_series_launches_episode_url(self, db, log, monkeypatch):
        _stub_resolve(monkeypatch, [source.EpisodeResolution(
            "playable", url="https://catalog.example.test/tv-shows/99/ep",
            episode_id="99")])
        sel = selection.select_playable([_series()], PROFILE, set(), db, log)
        assert sel.status == "playable"
        assert sel.playable_url == "https://catalog.example.test/tv-shows/99/ep"

    def test_series_never_launches_the_show_url(self, db, log, monkeypatch):
        # The show page mounts no video element; launching it would look
        # like a hang rather than a failure.
        _stub_resolve(monkeypatch, [
            source.EpisodeResolution("no_episodes")] * 3)
        sel = selection.select_playable([_series()], PROFILE, set(), db, log)
        assert sel.status != "playable"
        assert sel.playable_url is None

    def test_transient_series_is_not_launched(self, db, log, monkeypatch):
        # Deliberately unlike the movie policy: there is no playable URL yet.
        _stub_resolve(monkeypatch, [
            source.EpisodeResolution("transient_error")] * 4)
        pool = [_series(f"s{i}") for i in range(4)]
        sel = selection.select_playable(pool, PROFILE, set(), db, log)
        assert sel.status == "exhausted"
        assert sel.playable_url is None

    def test_confirmed_failure_is_recorded(self, db, log, monkeypatch):
        # Registry-sourced series: a real post-migration row, keyed
        # (content_type='series', video_id) -- which is what
        # record_verification(content_type='series') targets.
        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO movies (content_type, video_id, title, catalog_url, "
                "tags, first_seen_at, last_seen_at) VALUES "
                "('series','s1','S','https://catalog.example.test/series/s1/s',"
                "'[\"thriller\"]','2026-08-14T00:00:00Z','2026-08-14T00:00:00Z')")
            conn.commit()
        _stub_resolve(monkeypatch, [
            source.EpisodeResolution("no_episodes")] * 3)
        selection.select_playable([_series("s1")], PROFILE, set(), db, log)
        row = registry.get(db, "s1", content_type="series")
        assert row is not None and row["verification_status"] == "failed"

    def test_one_request_cannot_drive_a_title_to_unavailable(self, db, log,
                                                             monkeypatch):
        # A single dead candidate must be tested once, not once per attempt:
        # three confirmed failures in one request would otherwise trip
        # UNAVAILABLE_THRESHOLD off the back of a single serve.
        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO movies (content_type, video_id, title, catalog_url, "
                "tags, first_seen_at, last_seen_at) VALUES "
                "('series','solo','S','https://catalog.example.test/series/solo/s',"
                "'[\"thriller\"]','2026-08-14T00:00:00Z','2026-08-14T00:00:00Z')")
            conn.commit()
        _stub_resolve(monkeypatch, [
            source.EpisodeResolution("no_episodes")] * 3)
        sel = selection.select_playable([_series("solo")], PROFILE, set(),
                                        db, log)
        assert sel.tried == ["solo"]
        assert sel.status == "no_match"
        row = registry.get(db, "solo", content_type="series")
        assert row["verification_status"] == "failed"

    def test_transient_leaves_no_permanent_mark(self, db, log, monkeypatch):
        _stub_resolve(monkeypatch, [
            source.EpisodeResolution("transient_error")] * 3)
        selection.select_playable([_series("s1")], PROFILE, set(), db, log)
        row = registry.get(db, "s1", content_type="series")
        assert row is None or row["verification_status"] is None

    def test_parse_error_leaves_no_permanent_mark(self, db, log, monkeypatch):
        _stub_resolve(monkeypatch, [
            source.EpisodeResolution("parse_error")] * 3)
        selection.select_playable([_series("s1")], PROFILE, set(), db, log)
        row = registry.get(db, "s1", content_type="series")
        assert row is None or row["verification_status"] is None


    def test_live_grid_series_without_a_row_does_not_crash(self, db, log,
                                                           monkeypatch):
        # record_verification() only updates; a live-grid series has no row,
        # so the confirmed-failure mark is a no-op rather than an error.
        _stub_resolve(monkeypatch, [
            source.EpisodeResolution("no_episodes")] * 4)
        sel = selection.select_playable(
            [_series(f"ghost{i}") for i in range(4)], PROFILE, set(), db, log)
        assert sel.status == "exhausted"


class TestEpisodeSequencing:
    def test_first_play_passes_no_last_played_id(self, db, log, monkeypatch):
        captured = {}

        def fake_resolve(video, last_played_episode_id=None, **k):
            captured["last_played_episode_id"] = last_played_episode_id
            return source.EpisodeResolution(
                "playable", url="https://catalog.example.test/tv-shows/e1/x",
                episode_id="e1", season=1, episode_number=1)
        monkeypatch.setattr(source, "resolve_playable_episode", fake_resolve)

        selection.select_playable([_series("s1")], PROFILE, set(), db, log)
        assert captured["last_played_episode_id"] is None

    def test_launch_records_the_episode_play(self, db, log, monkeypatch):
        monkeypatch.setattr(
            source, "resolve_playable_episode",
            lambda *a, **k: source.EpisodeResolution(
                "playable", url="https://catalog.example.test/tv-shows/e1/x",
                episode_id="e1", season=1, episode_number=1))

        selection.select_playable([_series("s1")], PROFILE, set(), db, log)
        row = registry.get_last_played_episode(db, "s1")
        assert row == {"episode_id": "e1", "season": 1, "episode": 1,
                       "played_at": row["played_at"]}

    def test_second_play_passes_the_prior_episode_id(self, db, log, monkeypatch):
        registry.record_episode_play(db, "s1", "e1", season=1, episode=1)
        captured = {}

        def fake_resolve(video, last_played_episode_id=None, **k):
            captured["last_played_episode_id"] = last_played_episode_id
            return source.EpisodeResolution(
                "playable", url="https://catalog.example.test/tv-shows/e2/x",
                episode_id="e2", season=1, episode_number=2)
        monkeypatch.setattr(source, "resolve_playable_episode", fake_resolve)

        selection.select_playable([_series("s1")], PROFILE, set(), db, log)
        assert captured["last_played_episode_id"] == "e1"
        row = registry.get_last_played_episode(db, "s1")
        assert row["episode_id"] == "e2"

    def test_a_rejected_series_does_not_record_a_play(self, db, log, monkeypatch):
        _stub_resolve(monkeypatch, [
            source.EpisodeResolution("no_episodes")] * 3)
        selection.select_playable([_series("s1")], PROFILE, set(), db, log)
        assert registry.get_last_played_episode(db, "s1") is None


class TestBounds:
    def test_attempts_are_bounded_total_not_repicks(self, db, log, monkeypatch):
        import verify
        calls = []
        monkeypatch.setattr(
            verify, "verify_playable",
            lambda vid, url, **k: (calls.append(vid), verify.VerifyResult(
                status="failed", failure_reason="http_404"))[1])
        pool = [_movie(f"m{i}") for i in range(10)]
        sel = selection.select_playable(pool, PROFILE, set(), db, log)
        assert sel.status == "exhausted"
        assert len(calls) == selection.MAX_ATTEMPTS

    def test_a_dead_slug_does_not_walk_the_pool(self, db, log, monkeypatch):
        _stub_resolve(monkeypatch, [
            source.EpisodeResolution("no_episodes")] * 50)
        pool = [_series(f"s{i}") for i in range(40)]
        sel = selection.select_playable(pool, PROFILE, set(), db, log)
        assert len(sel.tried) == selection.MAX_ATTEMPTS

    def test_failed_candidate_excluded_from_repick(self, db, log, monkeypatch):
        _stub_resolve(monkeypatch, [
            source.EpisodeResolution("no_episodes")] * 3)
        pool = [_series("s1"), _series("s2"), _series("s3")]
        sel = selection.select_playable(pool, PROFILE, set(), db, log)
        assert len(set(sel.tried)) == len(sel.tried)

    def test_exhausted_is_reported_not_silent(self, db, log, monkeypatch):
        _stub_resolve(monkeypatch, [
            source.EpisodeResolution("no_episodes")] * 5)
        pool = [_series(f"s{i}") for i in range(5)]
        sel = selection.select_playable(pool, PROFILE, set(), db, log)
        assert sel.status == "exhausted"
        assert sel.attempts == selection.MAX_ATTEMPTS

    def test_no_match_when_pool_runs_out_before_attempts_do(self, db, log,
                                                            monkeypatch):
        # Fewer candidates than attempts: the pool is what runs out, and
        # that is a different outcome from spending every attempt.
        _stub_resolve(monkeypatch, [
            source.EpisodeResolution("no_episodes")] * 3)
        sel = selection.select_playable(
            [_series("a"), _series("b")], PROFILE, set(), db, log)
        assert sel.status == "no_match"
        assert len(sel.tried) == 2

    def test_no_match_when_nothing_clears_the_profile(self, db, log):
        sel = selection.select_playable([], PROFILE, set(), db, log)
        assert sel.status == "no_match"


class TestFallbackTypeRules:
    def test_live_grid_linear_is_dropped(self, db, log, monkeypatch):
        monkeypatch.setattr(source, "search_category", lambda slug, **k: [
            _movie("ok"), _movie("chan", detailed_type="linear")])
        out = selection.resolve_window_candidates(
            {"category_slugs": ["x"]}, db, log)
        assert {c["video_id"] for c in out} == {"ok"}

    def test_live_grid_series_is_kept(self, db, log, monkeypatch):
        monkeypatch.setattr(source, "search_category", lambda slug, **k: [
            _series("s1")])
        out = selection.resolve_window_candidates(
            {"category_slugs": ["x"]}, db, log)
        assert {c["video_id"] for c in out} == {"s1"}

    def test_max_year_filter_applies_to_fallback(self, db, log, monkeypatch):
        monkeypatch.setattr(source, "search_category", lambda slug, **k: [
            _movie("old", year=1995), _movie("new", year=2010)])
        out = selection.resolve_window_candidates(
            {"category_slugs": [{"slug": "x", "max_year": 2000}]}, db, log)
        assert {c["video_id"] for c in out} == {"old"}
