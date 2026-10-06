"""Tests for migrate_types.py."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import migrate_types as mig
import registry


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "registry_test.db"
    registry.init(p)
    return p


def _insert(db, video_id, *, title="Show", url="", duration=None,
            content_type="movie", tags="[]"):
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO movies (content_type, video_id, title, catalog_url, "
            "duration_seconds, tags, first_seen_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?,'2026-08-14T00:00:00Z','2026-08-14T00:00:00Z')",
            (content_type, video_id, title, url, duration, tags),
        )
        conn.commit()


def _seed_expected(db):
    """A registry matching the migration's expected cohort sizes."""
    for i in range(mig.EXPECTED_SERIES):
        _insert(db, f"0{i}", title=f"Series {i}", url="")
    for i in range(mig.EXPECTED_LINEAR):
        _insert(db, f"9{i}", title=f"Channel {i}",
                url=f"https://catalog.example.test/movies/9{i}/channel")
    _insert(db, "500", title="Real Movie", duration=5400,
            url="https://catalog.example.test/movies/500/real-movie")


class TestSeriesUrl:
    def test_strips_leading_zeros(self):
        assert mig.series_url("04294", "Dance Moms") == \
            "https://catalog.example.test/series/4294/dance-moms"

    def test_all_zero_id_survives(self):
        assert mig.series_url("000", "X").startswith("https://catalog.example.test/series/0/")

    def test_slugifies_punctuation(self):
        url = mig.series_url("01495", "Yu-Gi-Oh! 5D's")
        assert url == "https://catalog.example.test/series/1495/yu-gi-oh-5d-s"

    def test_missing_title_does_not_raise(self):
        assert mig.series_url("010", None).endswith("/series/10/")


class TestPreconditions:
    def test_accepts_expected_registry(self, db):
        _seed_expected(db)
        with sqlite3.connect(db) as conn:
            assert mig.check_preconditions(conn) == (
                mig.EXPECTED_SERIES, mig.EXPECTED_LINEAR)

    def test_aborts_on_unexpected_series_count(self, db):
        _seed_expected(db)
        _insert(db, "extra", title="Another Series", url="")
        with sqlite3.connect(db) as conn:
            with pytest.raises(mig.MigrationError, match="series rows"):
                mig.check_preconditions(conn)

    def test_aborts_when_series_type_already_present(self, db):
        _seed_expected(db)
        _insert(db, "777", content_type="series", duration=100,
                url="https://catalog.example.test/series/777/x")
        with sqlite3.connect(db) as conn:
            with pytest.raises(mig.MigrationError, match="primary key"):
                mig.check_preconditions(conn)

    def test_aborts_when_a_durationless_row_matches_neither_cohort(self, db):
        _seed_expected(db)
        # A /series/ URL with no duration belongs to neither rule.
        _insert(db, "888", url="https://catalog.example.test/series/888/x")
        with sqlite3.connect(db) as conn:
            with pytest.raises(mig.MigrationError, match="neither rule"):
                mig.check_preconditions(conn)

    def test_aborts_when_a_series_row_has_no_title(self, db):
        _seed_expected(db)
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE movies SET title=NULL WHERE video_id='00'")
            conn.commit()
        with sqlite3.connect(db) as conn:
            with pytest.raises(mig.MigrationError, match="no title"):
                mig.check_preconditions(conn)


class TestPostconditions:
    def test_passes_on_clean_migration(self, db):
        _insert(db, "1", content_type="series", title="S",
                url="https://catalog.example.test/series/1/s")
        _insert(db, "2", content_type="linear", title="C",
                url="https://catalog.example.test/movies/2/c")
        _insert(db, "3", duration=5400, url="https://catalog.example.test/movies/3/m")
        with sqlite3.connect(db) as conn:
            mig.check_postconditions(conn, 1, 1)  # must not raise

    def test_catches_durationless_movie(self, db):
        _insert(db, "3", url="https://catalog.example.test/movies/3/m")
        with sqlite3.connect(db) as conn:
            with pytest.raises(mig.MigrationError, match="no duration"):
                mig.check_postconditions(conn, 0, 0)

    def test_catches_series_without_series_url(self, db):
        _insert(db, "1", content_type="series", title="S",
                url="https://catalog.example.test/movies/1/s")
        with sqlite3.connect(db) as conn:
            with pytest.raises(mig.MigrationError, match="/series/ URL"):
                mig.check_postconditions(conn, 1, 0)

    def test_catches_unrecognized_content_type(self, db):
        _insert(db, "9", content_type="sports", duration=10,
                url="https://catalog.example.test/movies/9/x")
        with sqlite3.connect(db) as conn:
            with pytest.raises(mig.MigrationError, match="unrecognized"):
                mig.check_postconditions(conn, 0, 0)
