"""Tests for source.py's episode ordering: _parse_season_episode() and
resolve_playable_episode()'s next-episode sequencing.

Season identity only exists in the "S0X:EYY" title prefix (see
source.py's EPISODE ORDERING docstring) -- these tests fake
fetch_movie_page() rather than hitting the network, since the parsing and
sequencing logic is what's under test, not the HTTP layer.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import source


def _episode(episode_id, series_id, title):
    return {
        "detailed_type": "episode",
        "series_id": series_id,
        "title": title,
    }


def _show_page(episodes: dict[str, dict]) -> dict:
    """episodes: {episode_id: episode_dict}."""
    return {"video": {"byId": episodes}}


class TestParseSeasonEpisode:
    def test_parses_standard_prefix(self):
        assert source._parse_season_episode("S01:E01 - First Encounter") == (1, 1)

    def test_parses_double_digit_season(self):
        assert source._parse_season_episode("S12:E03 - Title") == (12, 3)

    def test_no_prefix_returns_none(self):
        assert source._parse_season_episode("Untitled Special") is None

    def test_empty_string_returns_none(self):
        assert source._parse_season_episode("") is None

    def test_none_title_returns_none(self):
        assert source._parse_season_episode(None) is None


class TestResolvePlayableEpisodeMovies:
    def test_movie_passes_through_without_fetching(self, monkeypatch):
        def fail(*a, **k):
            raise AssertionError("fetch_movie_page should not be called for a movie")
        monkeypatch.setattr(source, "fetch_movie_page", fail)
        video = {"detailed_type": "movie", "url": "https://catalog.example.test/movies/1/m"}
        result = source.resolve_playable_episode(video)
        assert result.status == "playable"
        assert result.url == video["url"]
        assert result.season is None


class TestResolvePlayableEpisodeSequencing:
    SERIES = {"video_id": "010", "detailed_type": "series",
              "url": "https://catalog.example.test/series/10/s"}

    def _stub(self, monkeypatch, episodes):
        monkeypatch.setattr(source, "fetch_movie_page",
                            lambda *a, **k: _show_page(episodes))

    def test_no_matching_episodes_is_no_episodes(self, monkeypatch):
        self._stub(monkeypatch, {})
        result = source.resolve_playable_episode(self.SERIES)
        assert result.status == "no_episodes"

    def test_fetch_failure_maps_to_not_found(self, monkeypatch):
        def raise_404(*a, **k):
            raise source.PageFetchError("not_found")
        monkeypatch.setattr(source, "fetch_movie_page", raise_404)
        result = source.resolve_playable_episode(self.SERIES)
        assert result.status == "not_found"

    def test_first_play_picks_earliest_episode(self, monkeypatch):
        self._stub(monkeypatch, {
            "e2": _episode("e2", "10", "S01:E02 - Two"),
            "e1": _episode("e1", "10", "S01:E01 - One"),
            "e3": _episode("e3", "10", "S02:E01 - Three"),
        })
        result = source.resolve_playable_episode(self.SERIES)
        assert result.episode_id == "e1"
        assert result.season == 1
        assert result.episode_number == 1

    def test_advances_to_next_episode_after_last_played(self, monkeypatch):
        self._stub(monkeypatch, {
            "e1": _episode("e1", "10", "S01:E01 - One"),
            "e2": _episode("e2", "10", "S01:E02 - Two"),
            "e3": _episode("e3", "10", "S02:E01 - Three"),
        })
        result = source.resolve_playable_episode(
            self.SERIES, last_played_episode_id="e1")
        assert result.episode_id == "e2"
        assert result.season == 1
        assert result.episode_number == 2

    def test_advances_across_season_boundary(self, monkeypatch):
        self._stub(monkeypatch, {
            "e1": _episode("e1", "10", "S01:E01 - One"),
            "e2": _episode("e2", "10", "S01:E02 - Two"),
            "e3": _episode("e3", "10", "S02:E01 - Three"),
        })
        result = source.resolve_playable_episode(
            self.SERIES, last_played_episode_id="e2")
        assert result.episode_id == "e3"
        assert result.season == 2
        assert result.episode_number == 1

    def test_wraps_to_earliest_after_series_finale(self, monkeypatch):
        self._stub(monkeypatch, {
            "e1": _episode("e1", "10", "S01:E01 - One"),
            "e2": _episode("e2", "10", "S02:E01 - Two"),
        })
        result = source.resolve_playable_episode(
            self.SERIES, last_played_episode_id="e2")
        assert result.episode_id == "e1"
        assert result.season == 1
        assert result.episode_number == 1

    def test_unknown_last_played_id_falls_back_to_earliest(self, monkeypatch):
        # e.g. the previously played episode left the catalog.
        self._stub(monkeypatch, {
            "e1": _episode("e1", "10", "S01:E01 - One"),
            "e2": _episode("e2", "10", "S01:E02 - Two"),
        })
        result = source.resolve_playable_episode(
            self.SERIES, last_played_episode_id="ghost")
        assert result.episode_id == "e1"

    def test_out_of_order_byid_still_sorts_correctly(self, monkeypatch):
        # Mirrors a real show observed live: byId iteration interleaves
        # seasons and episodes are not in dict order.
        self._stub(monkeypatch, {
            "e_s2e1": _episode("e_s2e1", "10", "S02:E01 - Episode 1"),
            "e_s2e3": _episode("e_s2e3", "10", "S02:E03 - Episode 3"),
            "e_s1e1": _episode("e_s1e1", "10", "S01:E01 - Episode 1"),
            "e_s2e2": _episode("e_s2e2", "10", "S02:E02 - Episode 2"),
        })
        first = source.resolve_playable_episode(self.SERIES)
        assert first.episode_id == "e_s1e1"
        second = source.resolve_playable_episode(
            self.SERIES, last_played_episode_id="e_s1e1")
        assert second.episode_id == "e_s2e1"
        third = source.resolve_playable_episode(
            self.SERIES, last_played_episode_id="e_s2e1")
        assert third.episode_id == "e_s2e2"

    def test_unparseable_titles_fall_back_to_dict_order_first_match(self, monkeypatch):
        self._stub(monkeypatch, {
            "e1": _episode("e1", "10", "Cold Open"),
            "e2": _episode("e2", "10", "The Reveal"),
        })
        result = source.resolve_playable_episode(self.SERIES)
        assert result.status == "playable"
        assert result.episode_id == "e1"
        assert result.season is None
        assert result.episode_number is None

    def test_mixed_parseable_and_not_ignores_unparseable_for_ordering(self, monkeypatch):
        self._stub(monkeypatch, {
            "special": _episode("special", "10", "Holiday Special"),
            "e1": _episode("e1", "10", "S01:E01 - One"),
        })
        result = source.resolve_playable_episode(self.SERIES)
        assert result.episode_id == "e1"

    def test_other_series_episodes_are_excluded(self, monkeypatch):
        self._stub(monkeypatch, {
            "e1": _episode("e1", "10", "S01:E01 - One"),
            "other": _episode("other", "999", "S01:E01 - Different show"),
        })
        result = source.resolve_playable_episode(self.SERIES)
        assert result.episode_id == "e1"

    def test_leading_zero_series_id_matches(self, monkeypatch):
        # video_id carries a leading zero (as real series ids do); the
        # episode's series_id field does not.
        self._stub(monkeypatch, {
            "e1": _episode("e1", "10", "S01:E01 - One"),
        })
        result = source.resolve_playable_episode(self.SERIES)
        assert result.episode_id == "e1"
