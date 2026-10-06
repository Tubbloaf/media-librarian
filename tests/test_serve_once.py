"""End-to-end serve path: serve_once().

Ported from the private system's end-to-end tests for its presence-triggered
handler. They exercise the real function the trigger calls -- daypart
selection, candidate resolution, series resolution, the movie fallback, and
what lands in recently-played -- rather than selection in isolation.

What is faked, and only that: the catalog grid (source.search_category),
episode resolution, and the verifier. The registry is a real SQLite file and
the launch is a callable that records the URL it was asked to launch.

One private test is not carried over: the one asserting that a serve marks
the trigger's session state. That state belongs to the trigger handler, not
to this pipeline.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import recently_played
import serve
import taste_select
import selection
import registry
import source
import verify


PROFILE = {
    "preferred_tags": {"crime": 5},
    "dayparts": [{
        "_window": "test", "min_hour": 0, "max_hour": 23,
        "tag_bonus": {"crime": 3},
        # Series outrank movies here so the fallback tests actually exhaust
        # the series before reaching a movie; with equal scores the
        # tie-break is random and the movie could win outright.
        "content_type_bonus": {"series": 5},
        "category_slugs": ["crime_tv"],
    }],
}


def _movie(video_id, **kw):
    base = {
        "video_id": video_id, "title": f"Movie {video_id}", "year": 2000,
        "duration_seconds": 5400, "tags": ["crime"], "categories": ["crime"],
        "stars": [], "directors": [], "imdb_rating": None,
        "rotten_tomatoes_score": None, "detailed_type": "movie",
        "url": f"https://catalog.example.test/movies/{video_id}/m",
    }
    return {**base, **kw}


def _series(video_id, **kw):
    return _movie(video_id, detailed_type="series",
                  url=f"https://catalog.example.test/series/{video_id}/s", **kw)


@pytest.fixture()
def serv(tmp_path, monkeypatch):
    """Wire the collaborators to fakes and return a runner."""
    db = tmp_path / "registry.db"
    registry.init(db)
    recent_path = tmp_path / "recently_played.json"

    launched = []
    logged = []
    # 1, not 0: at 0 an empty registry satisfies the floor and the live-grid
    # fallback never runs, which is the opposite of what these tests need.
    monkeypatch.setattr(selection, "MIN_SLUG_CANDIDATES", 1)

    def run(profile=PROFILE):
        return serve.serve_once(
            profile, db, launched.append,
            recently_played_path=recent_path, log=logged.append)

    return type("Served", (), dict(
        run=staticmethod(run), launched=launched, logged=logged,
        db=db, recent=property(lambda self: recently_played.load(recent_path))))()


def _grid(monkeypatch, items):
    monkeypatch.setattr(source, "search_category", lambda slug, **k: items)


def _resolve(monkeypatch, results):
    it = iter(results)
    monkeypatch.setattr(source, "resolve_playable_episode",
                        lambda *a, **k: next(it))


def _verify(monkeypatch, status="live"):
    monkeypatch.setattr(verify, "verify_playable",
                        lambda *a, **k: verify.VerifyResult(status=status))


class TestServePath:
    def test_movie_launches(self, serv, monkeypatch):
        _grid(monkeypatch, [_movie("m1")])
        _verify(monkeypatch)
        serv.run()
        assert serv.launched == ["https://catalog.example.test/movies/m1/m"]

    def test_series_launches_its_episode_url(self, serv, monkeypatch):
        _grid(monkeypatch, [_series("s1")])
        _resolve(monkeypatch, [source.EpisodeResolution(
            "playable", url="https://catalog.example.test/tv-shows/9/ep", episode_id="9")])
        serv.run()
        assert serv.launched == ["https://catalog.example.test/tv-shows/9/ep"]

    def test_recently_played_records_the_parent_not_the_episode(
            self, serv, monkeypatch):
        _grid(monkeypatch, [_series("s1")])
        _resolve(monkeypatch, [source.EpisodeResolution(
            "playable", url="https://catalog.example.test/tv-shows/9/ep", episode_id="9")])
        serv.run()
        assert serv.recent == ["s1"]
        assert "9" not in serv.recent

    def test_falls_back_to_a_movie_after_failed_series(self, serv, monkeypatch):
        # Zero-touch surface: silence reads as a broken room, so a movie is
        # substituted rather than nothing being launched.
        _grid(monkeypatch, [_series(f"s{i}") for i in range(3)] + [_movie("m9")])
        _resolve(monkeypatch, [
            source.EpisodeResolution("no_episodes")] * 3)
        _verify(monkeypatch)
        serv.run()
        assert serv.launched == ["https://catalog.example.test/movies/m9/m"]
        assert any("falling back to a movie" in line for line in serv.logged)

    def test_fallback_is_logged_not_silent(self, serv, monkeypatch):
        _grid(monkeypatch, [_series(f"s{i}") for i in range(3)])
        _resolve(monkeypatch, [
            source.EpisodeResolution("no_episodes")] * 3)
        serv.run()
        assert serv.launched == []
        assert any("falling back to a movie" in line for line in serv.logged)
        assert any("giving up this cycle" in line for line in serv.logged)

    def test_series_is_not_launched_on_transient_failure(self, serv, monkeypatch):
        # Unlike the movie policy: a series' stored URL is the show page.
        _grid(monkeypatch, [_series(f"s{i}") for i in range(3)])
        _resolve(monkeypatch, [
            source.EpisodeResolution("transient_error")] * 3)
        serv.run()
        assert serv.launched == []

    def test_api_fallback_supplies_candidates_when_registry_is_empty(
            self, serv, monkeypatch):
        _grid(monkeypatch, [_movie("api1")])
        _verify(monkeypatch)
        serv.run()
        assert serv.launched == ["https://catalog.example.test/movies/api1/m"]

    def test_registry_rows_are_preferred_over_the_grid(self, serv, monkeypatch):
        registry.observe_category(serv.db, [{
            "video_id": "reg1", "detailed_type": "movie", "tags": ["crime"],
            "duration_seconds": 5400,
            "catalog_url": "https://catalog.example.test/movies/reg1/m"}], "crime_tv")
        _grid(monkeypatch, [_movie("api1")])
        _verify(monkeypatch)
        serv.run()
        assert serv.launched == ["https://catalog.example.test/movies/reg1/m"]

    def test_linear_from_the_grid_is_never_launched(self, serv, monkeypatch):
        _grid(monkeypatch, [_movie("chan", detailed_type="linear")])
        _verify(monkeypatch)
        serv.run()
        assert serv.launched == []


class TestHourBoundary:
    """A request that straddles an hour must not change dayparts mid-flight.

    Window selection and scoring each read the clock for themselves until
    2026-08-20. They agree almost always and disagree exactly here -- the
    path fans out over several fetches between choosing a window and
    scoring what comes back, so a title could be sourced from one window's
    category_slugs and then scored under the next window's tag_bonus,
    short_max and content_type_bonus.

    Note for the port: in the private suite the last two tests below
    monkeypatched taste_select.load_profile, which the code under test never
    calls, so they ran against the single-window profile and could not fail
    for the reason they describe. Here the two-window profile is passed in.
    """

    TWO_WINDOWS = {
        "preferred_tags": {"crime": 5},
        "dayparts": [
            {"_window": "before", "min_hour": 9, "max_hour": 10,
             "tag_bonus": {"crime": 3}, "category_slugs": ["crime_tv"],
             "content_type_bonus": {"series": 5}},
            {"_window": "after", "min_hour": 10, "max_hour": 11,
             "tag_bonus": {"crime": -99}, "category_slugs": ["other"]},
        ],
    }

    def _advancing_clock(self, monkeypatch, hours):
        """taste_select.current_hour() returns each value in turn."""
        it = iter(hours)
        monkeypatch.setattr(taste_select, "current_hour", lambda: next(it))

    def _crossing_the_hour(self, monkeypatch):
        """First read says 9; every later read says 10."""
        calls = []

        def clock():
            calls.append(1)
            return 9 if len(calls) == 1 else 10
        monkeypatch.setattr(taste_select, "current_hour", clock)
        return calls

    def test_one_clock_read_per_request(self, serv, monkeypatch):
        # Only the first value is consumed; a second read would raise
        # StopIteration and fail this test.
        self._advancing_clock(monkeypatch, [9])
        _grid(monkeypatch, [_movie("m1")])
        _verify(monkeypatch)
        serv.run()
        assert serv.launched == ["https://catalog.example.test/movies/m1/m"]

    def test_scoring_uses_the_window_that_chose_the_candidates(
            self, serv, monkeypatch):
        # 'before' (hour 9) rewards crime; 'after' (hour 10) punishes it. If
        # scoring re-read the clock and landed in 'after', the comedy would
        # win instead.
        calls = self._crossing_the_hour(monkeypatch)
        _grid(monkeypatch, [_movie("crime1"),
                            _movie("comedy1", tags=["comedy"], categories=["comedy"])])
        _verify(monkeypatch)
        serv.run(self.TWO_WINDOWS)
        assert serv.launched == ["https://catalog.example.test/movies/crime1/m"]
        assert len(calls) == 1

    def test_fallback_repick_shares_the_same_hour(self, serv, monkeypatch):
        # The movie fallback runs after three series resolutions, each a
        # round trip -- the most likely place to cross an hour. Its re-pick
        # must still score under the hour that chose the candidates.
        calls = self._crossing_the_hour(monkeypatch)
        _grid(monkeypatch,
              [_series(f"s{i}") for i in range(3)]
              + [_movie("crime9"),
                 _movie("comedy9", tags=["comedy"], categories=["comedy"])])
        _resolve(monkeypatch, [
            source.EpisodeResolution("no_episodes")] * 3)
        _verify(monkeypatch)
        serv.run(self.TWO_WINDOWS)
        assert serv.launched == ["https://catalog.example.test/movies/crime9/m"]
        assert len(calls) == 1
