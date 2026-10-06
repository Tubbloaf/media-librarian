"""Tests for registry.py."""
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import registry


def _db() -> Path:
    """Temp DB path for a single test; caller must manage cleanup."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Path(tmp.name)


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "registry_test.db"
    registry.init(p)
    return p


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

class TestInit:
    def test_creates_table(self, db):
        import sqlite3
        with sqlite3.connect(db) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        assert "movies" in tables

    def test_reinit_is_safe(self, db):
        registry.init(db)  # should not raise


# ---------------------------------------------------------------------------
# register_from_sitemap
# ---------------------------------------------------------------------------

class TestRegisterFromSitemap:
    def _movie(self, video_id="111", title="Test Movie"):
        return {
            "video_id": video_id,
            "detailed_type": "movie",
            "title": title,
            "description": "A test.",
            "duration_seconds": 5400,
            "catalog_url": f"https://catalog.example.test/movies/{video_id}/test-movie",
            "thumbnail_url": "https://example.com/thumb.jpg",
            "sitemap_lastmod": "2026-08-14T07:30:00Z",
        }

    def test_inserts_new_row(self, db):
        registry.register_from_sitemap(db, [self._movie()])
        row = registry.get(db, "111")
        assert row is not None
        assert row["title"] == "Test Movie"
        assert row["duration_seconds"] == 5400

    def test_does_not_overwrite_title(self, db):
        registry.register_from_sitemap(db, [self._movie("222", "Original")])
        registry.register_from_sitemap(db, [self._movie("222", "Updated")])
        row = registry.get(db, "222")
        assert row["title"] == "Original"

    def test_updates_sitemap_lastmod(self, db):
        registry.register_from_sitemap(db, [self._movie("333")])
        registry.register_from_sitemap(db, [{**self._movie("333"),
                                                   "sitemap_lastmod": "2026-09-01T00:00:00Z"}])
        row = registry.get(db, "333")
        assert row["sitemap_lastmod"] == "2026-09-01T00:00:00Z"

    def test_first_seen_at_not_revised(self, db):
        registry.register_from_sitemap(db, [self._movie("444")])
        row1 = registry.get(db, "444")
        first = row1["first_seen_at"]
        registry.register_from_sitemap(db, [self._movie("444")])
        row2 = registry.get(db, "444")
        assert row2["first_seen_at"] == first

    def test_bulk_insert(self, db):
        movies = [self._movie(str(i)) for i in range(100)]
        registry.register_from_sitemap(db, movies)
        s = registry.stats(db)
        assert s["total"] == 100

    def test_initial_import_marks_all_as_baseline(self, db):
        registry.register_from_sitemap(
            db, [self._movie("777"), self._movie("778")], is_baseline=True
        )
        for vid in ["777", "778"]:
            assert registry.get(db, vid)["is_baseline_seed"] == 1

    def test_subsequent_import_marks_new_records_non_baseline(self, db):
        registry.register_from_sitemap(db, [self._movie("779")], is_baseline=True)
        registry.register_from_sitemap(db, [self._movie("780")], is_baseline=False)
        assert registry.get(db, "779")["is_baseline_seed"] == 1
        assert registry.get(db, "780")["is_baseline_seed"] == 0

    def test_upsert_preserves_baseline_status(self, db):
        registry.register_from_sitemap(db, [self._movie("781")], is_baseline=True)
        registry.register_from_sitemap(db, [self._movie("781")], is_baseline=False)
        assert registry.get(db, "781")["is_baseline_seed"] == 1

    def test_fills_null_title_from_second_call(self, db):
        # First call arrives from a category page (no title stored by sitemap yet)
        registry.observe_category(db, [{"video_id": "555", "detailed_type": "movie"}], "action")
        row = registry.get(db, "555")
        assert row["title"] is None
        # Sitemap import fills in the title
        registry.register_from_sitemap(db, [self._movie("555", "Found Title")])
        row = registry.get(db, "555")
        assert row["title"] == "Found Title"


# ---------------------------------------------------------------------------
# Baseline import lifecycle (multi-batch and interrupted bootstrap)
# ---------------------------------------------------------------------------

class TestBaselineImportBehavior:
    def _movie(self, video_id):
        return {
            "video_id": video_id,
            "detailed_type": "movie",
            "title": f"Movie {video_id}",
            "catalog_url": f"https://catalog.example.test/movies/{video_id}/m",
            "sitemap_lastmod": "2026-08-14T00:00:00+00:00",
        }

    def test_is_baseline_complete_false_initially(self, db):
        assert registry.is_baseline_complete(db) is False

    def test_mark_baseline_complete_sets_flag(self, db):
        registry.mark_baseline_complete(db)
        assert registry.is_baseline_complete(db) is True

    def test_mark_baseline_complete_is_idempotent(self, db):
        registry.mark_baseline_complete(db)
        registry.mark_baseline_complete(db)  # must not raise
        assert registry.is_baseline_complete(db) is True

    def test_multi_batch_bootstrap_all_marked_baseline(self, db):
        """Multiple register_from_sitemap(is_baseline=True) calls before
        mark_baseline_complete all produce is_baseline_seed=1 records."""
        registry.register_from_sitemap(db, [self._movie("900")], is_baseline=True)
        registry.register_from_sitemap(db, [self._movie("901")], is_baseline=True)
        registry.mark_baseline_complete(db)
        assert registry.get(db, "900")["is_baseline_seed"] == 1
        assert registry.get(db, "901")["is_baseline_seed"] == 1

    def test_post_baseline_records_are_non_baseline(self, db):
        """After mark_baseline_complete, new inserts get is_baseline_seed=0."""
        registry.register_from_sitemap(db, [self._movie("902")], is_baseline=True)
        registry.mark_baseline_complete(db)
        registry.register_from_sitemap(db, [self._movie("903")], is_baseline=False)
        assert registry.get(db, "902")["is_baseline_seed"] == 1
        assert registry.get(db, "903")["is_baseline_seed"] == 0

    def test_interrupted_bootstrap_rerun_completes_correctly(self, db):
        """If bootstrap is interrupted (marker never written), a rerun imports
        remaining records as is_baseline_seed=1 and then writes the marker."""
        # First partial run (one sitemap file fetched, marker not written)
        registry.register_from_sitemap(db, [self._movie("904")], is_baseline=True)
        assert registry.is_baseline_complete(db) is False

        # Rerun imports remaining file and writes marker
        registry.register_from_sitemap(db, [self._movie("905")], is_baseline=True)
        registry.mark_baseline_complete(db)
        assert registry.get(db, "904")["is_baseline_seed"] == 1
        assert registry.get(db, "905")["is_baseline_seed"] == 1

        # Subsequent refresh adds a genuinely new title as non-baseline
        registry.register_from_sitemap(db, [self._movie("906")], is_baseline=False)
        assert registry.get(db, "906")["is_baseline_seed"] == 0

    def test_rerun_upsert_preserves_baseline_status(self, db):
        """Re-registering an existing baseline record during a rerun does not
        change its is_baseline_seed (ON CONFLICT path omits that column)."""
        registry.register_from_sitemap(db, [self._movie("907")], is_baseline=True)
        registry.mark_baseline_complete(db)
        # Re-register "907" in a later refresh — is_baseline_seed must stay 1
        registry.register_from_sitemap(db, [self._movie("907")], is_baseline=False)
        assert registry.get(db, "907")["is_baseline_seed"] == 1


# ---------------------------------------------------------------------------
# observe_category
# ---------------------------------------------------------------------------

class TestObserveCategory:
    def _movie(self, video_id="100", **kwargs):
        base = {
            "video_id": video_id,
            "detailed_type": "movie",
            "title": "Crime Story",
            "year": 1995,
            "duration_seconds": 6000,
            "tags": ["Crime", "Thriller"],
            "actors": ["Al Pacino"],
            "imdb_rating": "7.5",
            "rotten_tomatoes_score": None,
            "catalog_url": f"https://catalog.example.test/movies/{video_id}/crime-story",
        }
        return {**base, **kwargs}

    def test_inserts_if_not_exists(self, db):
        registry.observe_category(db, [self._movie()], "crime")
        row = registry.get(db, "100")
        assert row is not None
        assert json.loads(row["tags"]) == ["crime", "thriller"]

    def test_tags_are_lowercased(self, db):
        registry.observe_category(db, [self._movie(tags=["Crime", "THRILLER"])], "crime")
        row = registry.get(db, "100")
        assert json.loads(row["tags"]) == ["crime", "thriller"]

    def test_tags_replace_not_accumulate(self, db):
        registry.observe_category(db, [self._movie(tags=["crime", "thriller"])], "crime")
        registry.observe_category(db, [self._movie(tags=["action"])], "action")
        row = registry.get(db, "100")
        assert json.loads(row["tags"]) == ["action"]

    def test_source_categories_accumulates(self, db):
        registry.observe_category(db, [self._movie()], "crime")
        registry.observe_category(db, [self._movie()], "thriller")
        row = registry.get(db, "100")
        cats = json.loads(row["source_categories"])
        assert "crime" in cats
        assert "thriller" in cats

    def test_source_categories_no_duplicates(self, db):
        registry.observe_category(db, [self._movie()], "crime")
        registry.observe_category(db, [self._movie()], "crime")
        row = registry.get(db, "100")
        cats = json.loads(row["source_categories"])
        assert cats.count("crime") == 1

    def test_imdb_rating_replaced(self, db):
        registry.observe_category(db, [self._movie(imdb_rating="7.5")], "crime")
        registry.observe_category(db, [self._movie(imdb_rating="8.1")], "crime")
        row = registry.get(db, "100")
        assert row["imdb_rating"] == pytest.approx(8.1)

    def test_year_is_first_good_value(self, db):
        registry.observe_category(db, [self._movie(year=1995)], "crime")
        registry.observe_category(db, [self._movie(year=2000)], "crime")
        row = registry.get(db, "100")
        assert row["year"] == 1995

    def test_does_not_touch_verification_status(self, db):
        registry.observe_category(db, [self._movie()], "crime")
        row = registry.get(db, "100")
        assert row["verification_status"] is None

    def test_never_stores_a_linear_channel(self, db, capsys):
        registry.observe_category(
            db, [self._movie(detailed_type="linear")], "entertainment")
        for ct in ("movie", "series", "linear"):
            assert registry.get(db, "100", content_type=ct) is None
        assert capsys.readouterr().err == ""

    def test_stores_series_under_its_own_content_type(self, db, capsys):
        registry.observe_category(
            db, [self._movie(detailed_type="series",
                             catalog_url="https://catalog.example.test/series/100/s")],
            "crime")
        assert registry.get(db, "100") is None  # not a movie
        row = registry.get(db, "100", content_type="series")
        assert row is not None
        assert row["catalog_url"] == "https://catalog.example.test/series/100/s"
        assert capsys.readouterr().err == ""

    def test_series_accumulates_source_categories(self, db):
        item = self._movie(detailed_type="series",
                           catalog_url="https://catalog.example.test/series/100/s")
        registry.observe_category(db, [item], "crime_tv")
        registry.observe_category(db, [item], "true_crime")
        row = registry.get(db, "100", content_type="series")
        assert set(json.loads(row["source_categories"])) == {"crime_tv",
                                                             "true_crime"}

    def test_series_without_duration_is_stored_anyway(self, db):
        # A series has no runtime; the movie invariant must not apply to it.
        registry.observe_category(
            db, [self._movie(detailed_type="series", duration_seconds=None,
                             catalog_url="https://catalog.example.test/series/100/s")],
            "crime_tv")
        row = registry.get(db, "100", content_type="series")
        assert row is not None and row["duration_seconds"] is None

    def test_same_id_can_be_both_a_movie_and_a_series(self, db):
        # (content_type, video_id) is the primary key, so the two must not
        # read or overwrite each other during the merge.
        registry.observe_category(db, [self._movie(
            detailed_type="movie", duration_seconds=5400,
            catalog_url="https://catalog.example.test/movies/100/m")], "crime")
        registry.observe_category(db, [self._movie(
            detailed_type="series", duration_seconds=None,
            catalog_url="https://catalog.example.test/series/100/s")], "crime_tv")
        movie = registry.get(db, "100")
        series = registry.get(db, "100", content_type="series")
        assert movie["duration_seconds"] == 5400
        assert series["duration_seconds"] is None
        assert movie["catalog_url"].startswith("https://catalog.example.test/movies/")
        assert series["catalog_url"].startswith("https://catalog.example.test/series/")

    def test_warns_and_skips_unrecognized_type(self, db, capsys):
        registry.observe_category(
            db, [self._movie(detailed_type="sports_event")], "sports")
        assert registry.get(db, "100") is None
        assert "unrecognized detailed_type" in capsys.readouterr().err

    def test_warns_and_skips_missing_type(self, db, capsys):
        # An absent value is how linear channels were stored as movies before
        # the filter existed; it must never be defaulted back to 'movie'.
        movie = self._movie()
        del movie["detailed_type"]
        registry.observe_category(db, [movie], "crime")
        assert registry.get(db, "100") is None
        assert "unrecognized detailed_type" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# observe_movie_page
# ---------------------------------------------------------------------------

class TestObserveMoviePage:
    def test_stores_availability_ends(self, db):
        registry.observe_movie_page(db, "200", {
            "availability_ends": "2026-09-01T03:59:00.000Z",
        })
        row = registry.get(db, "200")
        assert row["availability_ends"] == "2026-09-01T03:59:00.000Z"

    def test_stores_imdb_id(self, db):
        registry.observe_movie_page(db, "201", {"imdb_id": "tt0117060"})
        row = registry.get(db, "201")
        assert row["imdb_id"] == "tt0117060"

    def test_stores_directors(self, db):
        registry.observe_movie_page(db, "202", {"directors": ["Brian De Palma"]})
        row = registry.get(db, "202")
        assert json.loads(row["directors"]) == ["Brian De Palma"]

    def test_stores_country_lang_mpaa(self, db):
        registry.observe_movie_page(db, "203", {
            "country": "United States",
            "lang": "English",
            "mpaa_rating": "PG-13",
        })
        row = registry.get(db, "203")
        assert row["country"] == "United States"
        assert row["lang"] == "English"
        assert row["mpaa_rating"] == "PG-13"

    def test_stamps_movie_page_observed_at(self, db):
        registry.observe_movie_page(db, "204", {})
        row = registry.get(db, "204")
        assert row["movie_page_observed_at"] is not None

    def test_title_is_first_good_value(self, db):
        registry.observe_movie_page(db, "205", {"title": "First Title"})
        registry.observe_movie_page(db, "205", {"title": "Second Title"})
        row = registry.get(db, "205")
        assert row["title"] == "First Title"


# ---------------------------------------------------------------------------
# record_verification
# ---------------------------------------------------------------------------

class TestRecordVerification:
    def _seed(self, db, video_id):
        registry.observe_category(db, [{
            "video_id": video_id,
            "detailed_type": "movie",
            "tags": ["action"],
            "catalog_url": f"https://catalog.example.test/movies/{video_id}/test",
        }], "action")

    def test_live_resets_failures(self, db):
        self._seed(db, "300")
        # Simulate 2 prior failures
        registry.record_verification(db, "300", "failed", "https://t.co/300", "http_404")
        registry.record_verification(db, "300", "failed", "https://t.co/300", "http_404")
        registry.record_verification(db, "300", "live",   "https://t.co/300")
        row = registry.get(db, "300")
        assert row["verification_status"] == "live"
        assert row["consecutive_failures"] == 0
        assert row["failure_reason"] is None

    def test_failures_increment(self, db):
        self._seed(db, "301")
        registry.record_verification(db, "301", "failed", "https://t.co/301", "http_404")
        row = registry.get(db, "301")
        assert row["consecutive_failures"] == 1
        assert row["verification_status"] == "failed"

    def test_reaches_unavailable_threshold(self, db):
        self._seed(db, "302")
        for _ in range(registry.UNAVAILABLE_THRESHOLD):
            registry.record_verification(db, "302", "failed", "https://t.co/302", "http_404")
        row = registry.get(db, "302")
        assert row["verification_status"] == "unavailable"
        assert row["consecutive_failures"] == registry.UNAVAILABLE_THRESHOLD

    def test_transient_error_does_not_increment_failures(self, db):
        self._seed(db, "303")
        registry.record_verification(db, "303", "failed", "https://t.co/303", "http_404")
        failures_before = registry.get(db, "303")["consecutive_failures"]
        registry.record_verification(db, "303", "transient_error", "https://t.co/303")
        row = registry.get(db, "303")
        assert row["consecutive_failures"] == failures_before

    def test_transient_error_updates_status(self, db):
        self._seed(db, "304")
        registry.record_verification(db, "304", "live", "https://t.co/304")
        registry.record_verification(db, "304", "transient_error", "https://t.co/304")
        row = registry.get(db, "304")
        assert row["verification_status"] == "transient_error"
        assert row["consecutive_failures"] == 0

    def test_stores_failure_reason(self, db):
        self._seed(db, "305")
        registry.record_verification(db, "305", "failed", "https://t.co/305", "id_not_in_response")
        row = registry.get(db, "305")
        assert row["failure_reason"] == "id_not_in_response"

    def test_stores_last_verified_url(self, db):
        self._seed(db, "306")
        registry.record_verification(db, "306", "live", "https://catalog.example.test/movies/306/test")
        row = registry.get(db, "306")
        assert row["last_verified_url"] == "https://catalog.example.test/movies/306/test"

    def test_invalid_record_does_not_increment_failures(self, db):
        self._seed(db, "307")
        registry.record_verification(db, "307", "invalid_record", "", "no_url_stored")
        row = registry.get(db, "307")
        assert row["verification_status"] == "invalid_record"
        assert row["consecutive_failures"] == 0
        assert row["failure_reason"] == "no_url_stored"

    def test_invalid_record_after_failure_does_not_count(self, db):
        # A data-quality flag after a real failure should not push toward unavailable.
        self._seed(db, "308")
        registry.record_verification(db, "308", "failed", "https://t.co/308", "http_404")
        failures_before = registry.get(db, "308")["consecutive_failures"]
        registry.record_verification(db, "308", "invalid_record", "", "no_url_stored")
        row = registry.get(db, "308")
        assert row["consecutive_failures"] == failures_before


# ---------------------------------------------------------------------------
# candidates_for_pick
# ---------------------------------------------------------------------------

class TestCandidatesForPick:
    def _seed(self, db, video_id, tags=None, status=None, failures=0,
              duration_seconds=5400):
        registry.observe_category(db, [{
            "video_id": video_id,
            "detailed_type": "movie",
            "title": f"Movie {video_id}",
            "year": 2000,
            "duration_seconds": duration_seconds,
            "tags": tags or ["thriller"],
            "actors": [],
            "imdb_rating": "6.5",
            "catalog_url": f"https://catalog.example.test/movies/{video_id}/movie",
        }], "thriller")
        if status:
            import sqlite3
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "UPDATE movies SET verification_status=?, consecutive_failures=? "
                    "WHERE video_id=?",
                    (status, failures, video_id),
                )
                conn.commit()

    def test_includes_never_verified(self, db):
        self._seed(db, "400")
        candidates = registry.candidates_for_pick(db)
        ids = {c["video_id"] for c in candidates}
        assert "400" in ids

    def test_includes_live(self, db):
        self._seed(db, "401", status="live")
        candidates = registry.candidates_for_pick(db)
        ids = {c["video_id"] for c in candidates}
        assert "401" in ids

    def test_includes_failed(self, db):
        self._seed(db, "402", status="failed", failures=1)
        candidates = registry.candidates_for_pick(db)
        ids = {c["video_id"] for c in candidates}
        assert "402" in ids

    def test_excludes_unavailable(self, db):
        self._seed(db, "403", status="unavailable", failures=3)
        candidates = registry.candidates_for_pick(db)
        ids = {c["video_id"] for c in candidates}
        assert "403" not in ids

    def test_excludes_invalid_record(self, db):
        self._seed(db, "403b", status="invalid_record")
        candidates = registry.candidates_for_pick(db)
        ids = {c["video_id"] for c in candidates}
        assert "403b" not in ids

    def test_includes_transient_error(self, db):
        self._seed(db, "403c", status="transient_error")
        candidates = registry.candidates_for_pick(db)
        ids = {c["video_id"] for c in candidates}
        assert "403c" in ids

    def _clear_url(self, db, video_id):
        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE movies SET catalog_url='' WHERE video_id=?", (video_id,))
            conn.commit()

    def test_excludes_missing_url_with_legacy_failed_status(self, db):
        # Real shape of the 45 pre-'invalid_record' rows: enriched, no URL,
        # status 'failed' -- which is otherwise an included status.
        self._seed(db, "403d", status="failed", failures=1)
        self._clear_url(db, "403d")
        candidates = registry.candidates_for_pick(db)
        ids = {c["video_id"] for c in candidates}
        assert "403d" not in ids

    def test_excludes_missing_url_when_never_verified(self, db):
        self._seed(db, "403e")
        self._clear_url(db, "403e")
        candidates = registry.candidates_for_pick(db)
        ids = {c["video_id"] for c in candidates}
        assert "403e" not in ids

    def test_excludes_durationless_linear_channel(self, db):
        # the catalog's category grids carry linear channels shaped like movies:
        # a /movies/ URL (detailed_type isn't 'series', so _normalize's else
        # branch builds one), tags, and no runtime. verify_playable() calls
        # these 'live', so only the absent duration disqualifies them.
        self._seed(db, "403f", status="live", duration_seconds=None)
        candidates = registry.candidates_for_pick(db)
        ids = {c["video_id"] for c in candidates}
        assert "403f" not in ids

    def test_warns_when_sitemap_row_lacks_duration(self, db, capsys):
        self._seed(db, "403g", duration_seconds=None)
        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE movies SET sitemap_lastmod='2026-08-14T00:00:00Z' "
                "WHERE video_id='403g'")
            conn.commit()
        registry.candidates_for_pick(db)
        assert "duration invariant" in capsys.readouterr().err

    def test_no_warning_when_invariant_holds(self, db, capsys):
        self._seed(db, "403h")
        registry.candidates_for_pick(db)
        assert capsys.readouterr().err == ""

    def test_category_slugs_scopes_result(self, db):
        self._seed(db, "408", tags=["thriller"])
        registry.observe_category(db, [{
            "video_id": "409", "title": "Movie 409", "year": 2000,
            "detailed_type": "movie",
            "duration_seconds": 5400, "tags": ["martial arts"], "actors": [],
            "catalog_url": "https://catalog.example.test/movies/409/movie",
        }], "martial_arts")
        ids = {c["video_id"] for c in
               registry.candidates_for_pick(db, category_slugs=["martial_arts"])}
        assert ids == {"409"}

    def test_category_slugs_omitted_returns_all(self, db):
        self._seed(db, "410")
        ids = {c["video_id"] for c in registry.candidates_for_pick(db)}
        assert "410" in ids

    def test_category_slug_match_is_not_a_prefix_match(self, db):
        registry.observe_category(db, [{
            "video_id": "411", "title": "Movie 411", "year": 2000,
            "detailed_type": "movie",
            "duration_seconds": 5400, "tags": ["drama"], "actors": [],
            "catalog_url": "https://catalog.example.test/movies/411/movie",
        }], "drama_classics")
        ids = {c["video_id"] for c in
               registry.candidates_for_pick(db, category_slugs=["drama"])}
        assert "411" not in ids

    def _set_window(self, db, video_id, starts=None, ends=None):
        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE movies SET availability_starts=?, availability_ends=? "
                "WHERE video_id=?", (starts, ends, video_id))
            conn.commit()

    NOW = "2026-08-20T12:00:00.000Z"

    def test_excludes_expired(self, db):
        self._seed(db, "412")
        self._set_window(db, "412", ends="2026-08-19T23:59:59.000Z")
        ids = {c["video_id"] for c in
               registry.candidates_for_pick(db, now=self.NOW)}
        assert "412" not in ids

    def test_includes_unexpired(self, db):
        self._seed(db, "413")
        self._set_window(db, "413", ends="2026-09-01T03:59:00.000Z")
        ids = {c["video_id"] for c in
               registry.candidates_for_pick(db, now=self.NOW)}
        assert "413" in ids

    def test_includes_when_window_unknown(self, db):
        # ~87% of the pool has no availability data; absence must not exclude.
        self._seed(db, "414")
        ids = {c["video_id"] for c in
               registry.candidates_for_pick(db, now=self.NOW)}
        assert "414" in ids

    def test_excludes_not_yet_available(self, db):
        self._seed(db, "415")
        self._set_window(db, "415", starts="2026-09-15T00:00:00.000Z")
        ids = {c["video_id"] for c in
               registry.candidates_for_pick(db, now=self.NOW)}
        assert "415" not in ids

    def test_now_default_format_sorts_against_stored_values(self, db):
        # _now() renders '+00:00', which would not compare correctly against
        # a 'Z'-suffixed value in the same second.
        now = registry._now_availability()
        assert len(now) == 24 and now.endswith("Z")

    def _seed_typed(self, db, video_id, content_type, url, duration=None,
                    tags='["thriller"]'):
        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO movies (content_type, video_id, title, catalog_url, "
                "duration_seconds, tags, first_seen_at, last_seen_at) VALUES "
                "(?,?,?,?,?,?,'2026-08-14T00:00:00Z','2026-08-14T00:00:00Z')",
                (content_type, video_id, f"T {video_id}", url, duration, tags))
            conn.commit()

    def test_series_is_eligible(self, db):
        self._seed_typed(db, "500", "series",
                         "https://catalog.example.test/series/500/s")
        ids = {c["video_id"] for c in registry.candidates_for_pick(db)}
        assert "500" in ids

    def test_series_needs_no_duration(self, db):
        # A series has no runtime of its own; requiring one would exclude
        # every series, which is what the old universal rule did.
        self._seed_typed(db, "501", "series",
                         "https://catalog.example.test/series/501/s", duration=None)
        ids = {c["video_id"] for c in registry.candidates_for_pick(db)}
        assert "501" in ids

    def test_series_with_movies_url_is_excluded(self, db):
        self._seed_typed(db, "502", "series",
                         "https://catalog.example.test/movies/502/s")
        ids = {c["video_id"] for c in registry.candidates_for_pick(db)}
        assert "502" not in ids

    def test_linear_is_never_eligible(self, db):
        # Even carrying a duration and a /movies/ URL -- the shape that got
        # 172 channels into the pool in the first place.
        self._seed_typed(db, "503", "linear",
                         "https://catalog.example.test/movies/503/c", duration=5400)
        ids = {c["video_id"] for c in registry.candidates_for_pick(db)}
        assert "503" not in ids

    def test_unknown_content_type_is_never_eligible(self, db):
        self._seed_typed(db, "504", "sports_event",
                         "https://catalog.example.test/movies/504/x", duration=5400)
        ids = {c["video_id"] for c in registry.candidates_for_pick(db)}
        assert "504" not in ids

    def test_movie_with_zero_duration_is_excluded(self, db):
        # the catalog reports 0 for a linear channel, so a zero is a type error,
        # not a short film. The old IS NOT NULL test let this through.
        self._seed_typed(db, "505", "movie",
                         "https://catalog.example.test/movies/505/m", duration=0)
        ids = {c["video_id"] for c in registry.candidates_for_pick(db)}
        assert "505" not in ids

    def test_movie_with_series_url_is_excluded(self, db):
        self._seed_typed(db, "506", "movie",
                         "https://catalog.example.test/series/506/m", duration=5400)
        ids = {c["video_id"] for c in registry.candidates_for_pick(db)}
        assert "506" not in ids

    def test_content_types_can_be_narrowed(self, db):
        self._seed_typed(db, "507", "series",
                         "https://catalog.example.test/series/507/s")
        self._seed_typed(db, "508", "movie",
                         "https://catalog.example.test/movies/508/m", duration=5400)
        ids = {c["video_id"] for c in
               registry.candidates_for_pick(db, content_types=("movie",))}
        assert ids == {"508"}

    def test_candidate_reports_its_real_type(self, db):
        self._seed_typed(db, "509", "series",
                         "https://catalog.example.test/series/509/s")
        c = next(x for x in registry.candidates_for_pick(db)
                 if x["video_id"] == "509")
        assert c["detailed_type"] == "series"

    def test_excludes_unenriched(self, db):
        # Inserted from sitemap only — no tags
        registry.register_from_sitemap(db, [{
            "video_id": "404",
            "detailed_type": "movie",
            "title": "Bare Entry",
            "duration_seconds": 5400,
            "catalog_url": "https://catalog.example.test/movies/404/bare",
            "sitemap_lastmod": "2026-08-14T00:00:00Z",
        }])
        candidates = registry.candidates_for_pick(db)
        ids = {c["video_id"] for c in candidates}
        assert "404" not in ids

    def test_exclude_ids_honored(self, db):
        self._seed(db, "405")
        self._seed(db, "406")
        candidates = registry.candidates_for_pick(db, exclude_ids={"405"})
        ids = {c["video_id"] for c in candidates}
        assert "405" not in ids
        assert "406" in ids

    def test_candidate_shape_has_required_keys(self, db):
        self._seed(db, "407")
        candidates = registry.candidates_for_pick(db)
        c = next(x for x in candidates if x["video_id"] == "407")
        for key in ("video_id", "title", "url", "year", "tags", "categories",
                    "stars", "directors", "imdb_rating", "detailed_type"):
            assert key in c, f"missing key: {key}"

    def test_tags_equal_categories(self, db):
        self._seed(db, "408", tags=["sci-fi", "thriller"])
        candidates = registry.candidates_for_pick(db)
        c = next(x for x in candidates if x["video_id"] == "408")
        assert c["tags"] == c["categories"]


# ---------------------------------------------------------------------------
# pending_verification
# ---------------------------------------------------------------------------

class TestPendingVerification:
    def _seed_enriched(self, db, video_id):
        registry.observe_category(db, [{
            "video_id": video_id,
            "detailed_type": "movie",
            "tags": ["action"],
            "catalog_url": f"https://catalog.example.test/movies/{video_id}/movie",
        }], "action")

    def test_never_checked_enriched_appears_first(self, db):
        self._seed_enriched(db, "500")
        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE movies SET verification_status='live', last_verified_at=datetime('now') WHERE video_id='500'")
            conn.commit()
        self._seed_enriched(db, "501")  # never checked, enriched

        queue = registry.pending_verification(db)
        ids = [item["video_id"] for item in queue]
        assert "501" in ids
        # 500 is live and recently verified; should NOT appear in queue
        assert "500" not in ids

    def test_stale_live_appears_in_queue(self, db):
        self._seed_enriched(db, "502")
        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE movies SET verification_status='live', "
                "last_verified_at=datetime('now','-8 days') WHERE video_id='502'"
            )
            conn.commit()
        queue = registry.pending_verification(db, limit=50)
        ids = [item["video_id"] for item in queue]
        assert "502" in ids

    def test_unavailable_with_old_check_appears(self, db):
        self._seed_enriched(db, "503")
        import sqlite3
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE movies SET verification_status='unavailable', "
                "last_verified_at=datetime('now','-31 days') WHERE video_id='503'"
            )
            conn.commit()
        queue = registry.pending_verification(db, limit=50)
        ids = [item["video_id"] for item in queue]
        assert "503" in ids

    def test_recently_verified_live_not_in_queue(self, db):
        self._seed_enriched(db, "504")
        registry.record_verification(db, "504", "live", "https://catalog.example.test/movies/504/m")
        queue = registry.pending_verification(db, limit=50)
        ids = [item["video_id"] for item in queue]
        assert "504" not in ids

    def test_invalid_record_not_in_queue(self, db):
        self._seed_enriched(db, "505")
        registry.record_verification(db, "505", "invalid_record", "", "no_url_stored")
        queue = registry.pending_verification(db, limit=50)
        ids = [item["video_id"] for item in queue]
        assert "505" not in ids


# ---------------------------------------------------------------------------
# record_serve / recently_served
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# record_page_fetch_failure / record_page_fetch_success
# ---------------------------------------------------------------------------

class TestPageFetchTracking:
    def _seed(self, db, video_id, sitemap_lastmod="2026-08-14T00:00:00+00:00"):
        registry.register_from_sitemap(db, [{
            "video_id":        video_id,
            "detailed_type": "movie",
            "title":           f"Movie {video_id}",
            "catalog_url":        f"https://catalog.example.test/movies/{video_id}/m",
            "sitemap_lastmod": sitemap_lastmod,
        }])

    def test_failure_increments_consecutive_failures(self, db):
        self._seed(db, "700")
        registry.record_page_fetch_failure(db, "700", "not_found")
        row = registry.get(db, "700")
        assert row["movie_page_consecutive_failures"] == 1

    def test_second_failure_increments_again(self, db):
        self._seed(db, "701")
        registry.record_page_fetch_failure(db, "701", "transient_error")
        registry.record_page_fetch_failure(db, "701", "transient_error")
        row = registry.get(db, "701")
        assert row["movie_page_consecutive_failures"] == 2

    def test_failure_sets_error_type(self, db):
        self._seed(db, "702")
        registry.record_page_fetch_failure(db, "702", "not_found")
        row = registry.get(db, "702")
        assert row["movie_page_fetch_error"] == "not_found"

    def test_failure_sets_retry_after(self, db):
        self._seed(db, "703")
        registry.record_page_fetch_failure(db, "703", "not_found")
        row = registry.get(db, "703")
        assert row["movie_page_retry_after"] is not None

    def test_failure_sets_last_attempted_at(self, db):
        self._seed(db, "703b")
        registry.record_page_fetch_failure(db, "703b", "transient_error")
        row = registry.get(db, "703b")
        assert row["movie_page_last_attempted_at"] is not None

    def test_success_resets_consecutive_failures(self, db):
        self._seed(db, "704")
        registry.record_page_fetch_failure(db, "704", "transient_error")
        registry.record_page_fetch_failure(db, "704", "transient_error")
        registry.record_page_fetch_success(db, "704", {})
        row = registry.get(db, "704")
        assert row["movie_page_consecutive_failures"] == 0

    def test_success_clears_fetch_error(self, db):
        self._seed(db, "705")
        registry.record_page_fetch_failure(db, "705", "not_found")
        registry.record_page_fetch_success(db, "705", {})
        row = registry.get(db, "705")
        assert row["movie_page_fetch_error"] is None

    def test_success_clears_retry_after(self, db):
        self._seed(db, "706")
        registry.record_page_fetch_failure(db, "706", "not_found")
        registry.record_page_fetch_success(db, "706", {})
        row = registry.get(db, "706")
        assert row["movie_page_retry_after"] is None

    def test_success_sets_movie_page_observed_at(self, db):
        self._seed(db, "707")
        registry.record_page_fetch_success(db, "707", {"title": "Test"})
        row = registry.get(db, "707")
        assert row["movie_page_observed_at"] is not None

    def test_success_stores_catalog_updated_at(self, db):
        self._seed(db, "708")
        registry.record_page_fetch_success(
            db, "708", {"catalog_updated_at": "2026-08-01T00:00:00+00:00"}
        )
        row = registry.get(db, "708")
        assert row["catalog_updated_at"] == "2026-08-01T00:00:00+00:00"

    def test_observe_movie_page_does_not_reset_fetch_error(self, db):
        # observe_movie_page (used for verify bonus update) must not clear error state
        self._seed(db, "709")
        registry.record_page_fetch_failure(db, "709", "not_found")
        registry.observe_movie_page(
            db, "709", {"availability_ends": "2026-09-01T00:00:00Z"}
        )
        row = registry.get(db, "709")
        assert row["movie_page_fetch_error"] == "not_found"
        assert row["movie_page_consecutive_failures"] == 1

    def test_not_found_has_longer_retry_than_transient(self, db):
        self._seed(db, "710")
        self._seed(db, "711")
        registry.record_page_fetch_failure(db, "710", "not_found")
        registry.record_page_fetch_failure(db, "711", "transient_error")
        row710 = registry.get(db, "710")
        row711 = registry.get(db, "711")
        assert row710["movie_page_retry_after"] > row711["movie_page_retry_after"]


# ---------------------------------------------------------------------------
# pending_movie_page_enrichment / movie_page_enrichment_stats
# ---------------------------------------------------------------------------

class TestPendingMoviePageEnrichment:
    def _seed_sitemap(self, db, video_id, baseline=False):
        """Seed a sitemap-only record.
        baseline=True → is_baseline_seed=1 (Tier 4).
        baseline=False (default) → is_baseline_seed=0 (Tier 3 when first_seen_at is recent)."""
        registry.register_from_sitemap(db, [{
            "video_id":        video_id,
            "detailed_type": "movie",
            "title":           f"Movie {video_id}",
            "catalog_url":        f"https://catalog.example.test/movies/{video_id}/m",
            "sitemap_lastmod": "2026-08-14T00:00:00+00:00",
        }], is_baseline=baseline)

    def _seed_category(self, db, video_id):
        registry.observe_category(db, [{
            "video_id": video_id,
            "detailed_type": "movie",
            "tags":     ["action"],
            "url":      f"https://catalog.example.test/movies/{video_id}/m",
        }], "action")

    def test_tier1_category_enriched_no_page(self, db):
        self._seed_category(db, "800")
        rows = registry.pending_movie_page_enrichment(db, budget=10)
        ids = {r["video_id"] for r in rows}
        assert "800" in ids

    def test_tier1_has_tier_value_1(self, db):
        self._seed_category(db, "801")
        rows = registry.pending_movie_page_enrichment(db, budget=10)
        r = next(x for x in rows if x["video_id"] == "801")
        assert r["tier"] == 1

    def test_tier3_fresh_discovery(self, db):
        # is_baseline_seed=0, first_seen_at=now → Tier 3
        self._seed_sitemap(db, "808")
        rows = registry.pending_movie_page_enrichment(db, budget=10, recent_days=7)
        r = next((x for x in rows if x["video_id"] == "808"), None)
        assert r is not None
        assert r["tier"] == 3

    def test_tier4_baseline_seed(self, db):
        # is_baseline_seed=1 → excluded from Tier 3, falls to Tier 4
        self._seed_sitemap(db, "809", baseline=True)
        rows = registry.pending_movie_page_enrichment(db, budget=10, recent_days=7)
        r = next((x for x in rows if x["video_id"] == "809"), None)
        assert r is not None
        assert r["tier"] == 4

    def test_tier1_before_tier3_before_tier4(self, db):
        self._seed_category(db, "810")               # tier 1
        self._seed_sitemap(db, "811")                # tier 3 (fresh discovery)
        self._seed_sitemap(db, "812", baseline=True) # tier 4 (baseline seed)
        rows = registry.pending_movie_page_enrichment(db, budget=10)
        ids = [r["video_id"] for r in rows]
        assert ids.index("810") < ids.index("811")
        assert ids.index("811") < ids.index("812")

    def test_already_page_enriched_not_in_tier1(self, db):
        self._seed_category(db, "820")
        registry.record_page_fetch_success(db, "820", {})
        rows = registry.pending_movie_page_enrichment(db, budget=10)
        ids = {r["video_id"] for r in rows}
        assert "820" not in ids

    def test_retry_after_in_future_excluded(self, db):
        self._seed_sitemap(db, "830", baseline=True)
        registry.record_page_fetch_failure(db, "830", "not_found")
        rows = registry.pending_movie_page_enrichment(db, budget=10)
        ids = {r["video_id"] for r in rows}
        assert "830" not in ids

    def test_retry_failed_overrides_retry_after(self, db):
        self._seed_sitemap(db, "831", baseline=True)
        registry.record_page_fetch_failure(db, "831", "not_found")
        rows = registry.pending_movie_page_enrichment(
            db, budget=10, retry_failed=True
        )
        ids = {r["video_id"] for r in rows}
        assert "831" in ids

    def test_priority_filter_limits_to_tier(self, db):
        self._seed_category(db, "840")                # tier 1
        self._seed_sitemap(db, "841", baseline=True)  # tier 4 (baseline seed)
        rows = registry.pending_movie_page_enrichment(
            db, budget=10, priority=4
        )
        ids = {r["video_id"] for r in rows}
        assert "840" not in ids   # tier 1, filtered out
        assert "841" in ids       # tier 4, included

    def test_budget_limits_results(self, db):
        for vid in ["850", "851", "852", "853", "854"]:
            self._seed_category(db, vid)
        rows = registry.pending_movie_page_enrichment(db, budget=3)
        assert len(rows) == 3

    def test_enrichment_stats_returns_tier_counts(self, db):
        self._seed_category(db, "860")               # tier 1
        self._seed_category(db, "861")               # tier 1
        self._seed_sitemap(db, "862", baseline=True) # tier 4 (baseline seed)
        stats = registry.movie_page_enrichment_stats(db)
        assert stats.get(1, 0) >= 2
        assert stats.get(4, 0) >= 1

    def test_enrichment_stats_excludes_backoff(self, db):
        self._seed_sitemap(db, "870", baseline=True)
        registry.record_page_fetch_failure(db, "870", "not_found")
        stats = registry.movie_page_enrichment_stats(db)
        # 404'd baseline record is in backoff — must not appear in tier 4
        assert stats.get(4, 0) == 0


class TestRecordServe:
    def test_increments_serve_count(self, db):
        registry.record_serve(db, "600")
        registry.record_serve(db, "600")
        row = registry.get(db, "600")
        assert row["serve_count"] == 2

    def test_recently_served_returns_ids(self, db):
        registry.record_serve(db, "601")
        registry.record_serve(db, "602")
        recent = registry.recently_served(db)
        assert "601" in recent
        assert "602" in recent

    def test_recently_served_respects_limit(self, db):
        for i in range(25):
            registry.record_serve(db, str(700 + i))
        recent = registry.recently_served(db, limit=10)
        assert len(recent) == 10


class TestEpisodePlays:
    def test_no_row_returns_none(self, db):
        assert registry.get_last_played_episode(db, "s1") is None

    def test_records_and_retrieves(self, db):
        registry.record_episode_play(db, "s1", "ep5", season=1, episode=5)
        row = registry.get_last_played_episode(db, "s1")
        assert row["episode_id"] == "ep5"
        assert row["season"] == 1
        assert row["episode"] == 5

    def test_second_play_overwrites_not_duplicates(self, db):
        registry.record_episode_play(db, "s1", "ep5", season=1, episode=5)
        registry.record_episode_play(db, "s1", "ep6", season=1, episode=6)
        row = registry.get_last_played_episode(db, "s1")
        assert row["episode_id"] == "ep6"
        import sqlite3
        with sqlite3.connect(db) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM episode_plays WHERE series_id = 's1'"
            ).fetchone()[0]
        assert count == 1

    def test_different_series_tracked_independently(self, db):
        registry.record_episode_play(db, "s1", "ep5", season=1, episode=5)
        registry.record_episode_play(db, "s2", "ep1", season=1, episode=1)
        assert registry.get_last_played_episode(db, "s1")["episode_id"] == "ep5"
        assert registry.get_last_played_episode(db, "s2")["episode_id"] == "ep1"
