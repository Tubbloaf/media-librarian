"""Tests for ingest_warnings.py."""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import ingest_warnings


def _warns():
    buf = io.StringIO()
    return ingest_warnings.Warnings("category 'crime_tv'", stream=buf), buf


class TestWarnings:
    def test_silent_when_nothing_added(self):
        w, buf = _warns()
        w.flush()
        assert buf.getvalue() == ""

    def test_one_line_per_kind_not_per_item(self):
        w, buf = _warns()
        for vid in ("1", "2", "3", "4"):
            w.add("linear channel", vid)
        w.flush()
        assert buf.getvalue().count("\n") == 1

    def test_reports_count_and_examples(self):
        w, buf = _warns()
        for vid in ("1", "2", "3", "4"):
            w.add("linear channel", vid)
        w.flush()
        out = buf.getvalue()
        assert "4 items" in out
        assert "e.g. 1, 2, 3" in out
        assert "4" not in out.split("e.g.")[1]  # capped at MAX_EXAMPLES

    def test_singular_for_one_item(self):
        w, buf = _warns()
        w.add("linear channel", "1")
        w.flush()
        assert "1 item," in buf.getvalue()

    def test_kinds_stay_separate(self):
        w, buf = _warns()
        w.add("linear channel", "1")
        w.add("missing duration", "2")
        w.flush()
        assert buf.getvalue().count("\n") == 2

    def test_context_is_included(self):
        w, buf = _warns()
        w.add("linear channel", "1")
        w.flush()
        assert "category 'crime_tv'" in buf.getvalue()

    def test_flush_resets(self):
        w, buf = _warns()
        w.add("linear channel", "1")
        w.flush()
        w.flush()
        assert buf.getvalue().count("\n") == 1

    def test_works_without_example_ids(self):
        w, buf = _warns()
        w.add("linear channel")
        w.flush()
        assert "1 item" in buf.getvalue()
