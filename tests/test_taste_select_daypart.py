"""Daypart bonus behaviour: short_bonus and content_type_bonus.

These two are deliberately independent. The 9-11am window wants
TV-length viewing; a short movie satisfies that as well as an episode
does, so short_bonus is the mechanism for short movies rather than a
proxy for "is this TV" that content_type_bonus replaces.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import taste_select


PROFILE = {
    "dayparts": [{
        "_window": "test",
        "min_hour": 9,
        "max_hour": 11,
        "tag_bonus": {"crime": 3},
        "short_max_minutes": 60,
        "short_bonus": 2,
        "content_type_bonus": {"series": 3},
    }],
}


def _v(**kw):
    base = {"tags": ["crime"], "duration_seconds": 5400,
            "detailed_type": "movie"}
    return {**base, **kw}


class TestContentTypeBonus:
    def test_series_receives_the_bonus(self):
        assert taste_select._daypart_bonus(
            _v(detailed_type="series", duration_seconds=None),
            PROFILE, hour=10) == 6.0

    def test_movie_does_not_receive_it(self):
        assert taste_select._daypart_bonus(_v(), PROFILE, hour=10) == 3.0

    def test_absent_config_is_a_no_op(self):
        profile = {"dayparts": [{"min_hour": 9, "max_hour": 11,
                                 "tag_bonus": {"crime": 3}}]}
        assert taste_select._daypart_bonus(
            _v(detailed_type="series"), profile, hour=10) == 3.0

    def test_unlisted_type_scores_nothing_extra(self):
        assert taste_select._daypart_bonus(
            _v(detailed_type="linear"), PROFILE, hour=10) == 3.0

    def test_outside_the_window_neither_applies(self):
        assert taste_select._daypart_bonus(
            _v(detailed_type="series"), PROFILE, hour=3) == 0.0

    def test_needs_no_duration(self):
        # A series' runtime is unknowable without resolving it, and
        # resolution happens after scoring.
        assert taste_select._daypart_bonus(
            _v(detailed_type="series", duration_seconds=None),
            PROFILE, hour=10) == 6.0


class TestShortBonusStillApplies:
    def test_short_movie_keeps_its_bonus(self):
        assert taste_select._daypart_bonus(
            _v(duration_seconds=2700), PROFILE, hour=10) == 5.0

    def test_long_movie_gets_no_short_bonus(self):
        assert taste_select._daypart_bonus(
            _v(duration_seconds=5400), PROFILE, hour=10) == 3.0

    def test_the_two_bonuses_are_independent(self):
        # A short movie and a series reach the window by different routes
        # and neither implies the other.
        short_movie = taste_select._daypart_bonus(
            _v(duration_seconds=2700), PROFILE, hour=10)
        series = taste_select._daypart_bonus(
            _v(detailed_type="series", duration_seconds=None),
            PROFILE, hour=10)
        assert short_movie == 5.0 and series == 6.0

    def test_both_apply_when_a_series_somehow_has_a_short_runtime(self):
        # Series carry no duration today, so this cannot arise from the
        # registry; pinned so the interaction is a decision, not an accident.
        assert taste_select._daypart_bonus(
            _v(detailed_type="series", duration_seconds=1800),
            PROFILE, hour=10) == 8.0
