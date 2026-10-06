"""feedback.py: counters, the clamp, and the read path's tolerance."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import feedback
import taste_select


def _v(video_id, **kw):
    return {"video_id": video_id, "tags": [], "categories": [], "stars": [],
            "detailed_type": "movie", **kw}


class TestAdjustment:
    def test_no_feedback_is_zero(self):
        assert feedback.adjustment(0, 0) == 0

    def test_a_skip_outweighs_a_positive(self):
        assert feedback.adjustment(1, 1) < 0

    def test_bonus_is_clamped(self):
        assert feedback.adjustment(100, 0) == feedback.MAX_BONUS

    def test_penalty_is_clamped(self):
        assert feedback.adjustment(0, 100) == -feedback.MAX_PENALTY


class TestRecording:
    def test_unknown_outcome_is_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            feedback.record_outcome(tmp_path / "r.db", "1", "meh")

    def test_outcomes_accumulate_per_title(self, tmp_path):
        db = tmp_path / "r.db"
        feedback.record_outcome(db, "1", "skip")
        feedback.record_outcome(db, "1", "skip")
        feedback.record_outcome(db, "1", "positive")
        feedback.record_outcome(db, "2", "positive")
        adj = feedback.adjustments(db, [_v("1"), _v("2"), _v("3")])
        assert adj == {"1": feedback.adjustment(1, 2), "2": feedback.adjustment(1, 0)}

    def test_content_type_is_part_of_identity(self, tmp_path):
        db = tmp_path / "r.db"
        feedback.record_outcome(db, "7", "skip", content_type="series")
        assert feedback.adjustments(db, [_v("7")]) == {}                      # the movie
        assert feedback.adjustments(db, [_v("7", detailed_type="series")]) \
            == {"7": feedback.adjustment(0, 1)}


class TestReadPath:
    def test_missing_database_is_no_feedback(self, tmp_path):
        assert feedback.adjustments(tmp_path / "none.db", [_v("1")]) == {}

    def test_database_without_the_table_is_no_feedback(self, tmp_path):
        import sqlite3
        db = tmp_path / "r.db"
        sqlite3.connect(db).close()
        assert feedback.adjustments(db, [_v("1")]) == {}


class TestPickIntegration:
    PROFILE = {"preferred_tags": {"a": 3}}

    def test_empty_feedback_leaves_scoring_unchanged(self):
        vids = [_v("1", tags=["a"]), _v("2")]
        assert taste_select.pick(vids, self.PROFILE, hour=12)["video_id"] == "1"
        assert taste_select.pick(vids, self.PROFILE, hour=12, feedback={})["video_id"] == "1"

    def test_adjustment_can_overturn_the_ranking(self):
        vids = [_v("1", tags=["a"]), _v("2")]
        assert taste_select.pick(
            vids, self.PROFILE, hour=12, feedback={"1": -5})["video_id"] == "2"
