"""Tests for verify.py -- the playability verifier.

The verifier is deterministic and reads declared playback behavior from the
fixture catalog, so these tests check the status policy, which is what the
registry depends on: which outcomes are definitive failures (and so count
toward 'unavailable') and which are not.

    live             -> playable
    failed           -> definitive; counts toward consecutive_failures
    transient_error  -> network trouble; does NOT count

The page-parsing tests the private verifier has (malformed blobs, missing
markers, undefined tokens) are not carried over: there is no page to parse.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import registry
import verify

URL = "https://catalog.example.test/movies/{id}/x"


def check(video_id):
    return verify.verify_playable(video_id, URL.format(id=video_id))


class TestLiveTitle:
    def test_live_status(self):
        assert check("100002").status == "live"

    def test_failure_reason_is_none(self):
        assert check("100002").failure_reason is None

    def test_final_url_is_the_url_reached(self):
        assert check("100002").final_url == URL.format(id="100002")

    def test_availability_window_is_reported_when_declared(self):
        r = check("100001")
        assert r.availability_starts == "2026-01-01T00:00:00.000Z"
        assert r.availability_ends == "2099-01-01T00:00:00.000Z"

    def test_availability_is_none_when_not_declared(self):
        r = check("100002")
        assert r.availability_starts is None and r.availability_ends is None


class TestDefinitiveFailures:
    @pytest.mark.parametrize("video_id, reason", [
        ("100003", "http_404"),
        ("100102", "http_error"),
        ("100007", "login_redirect"),
        ("100011", "parse_error"),
        ("100015", "id_not_in_response"),
    ])
    def test_failed_with_reason(self, video_id, reason):
        r = check(video_id)
        assert r.status == "failed"
        assert r.failure_reason == reason

    def test_login_redirect_reports_the_login_url(self):
        assert "login" in check("100007").final_url

    def test_an_id_the_catalog_does_not_list_is_id_not_in_response(self):
        r = check("999999")
        assert (r.status, r.failure_reason) == ("failed", "id_not_in_response")


class TestTransientFailure:
    def test_transient_error_status(self):
        r = check("100018")
        assert r.status == "transient_error"
        assert r.failure_reason == "transient_error"


class TestPolicyAgainstTheRegistry:
    """The distinction that matters: a blip must not retire a title."""

    @pytest.fixture()
    def db(self, tmp_path):
        db = tmp_path / "r.db"
        registry.init(db)
        registry.observe_category(db, [{
            "video_id": "100018", "detailed_type": "movie", "tags": ["comedy"],
            "duration_seconds": 5400, "catalog_url": URL.format(id="100018")}], "comedy")
        registry.observe_category(db, [{
            "video_id": "100003", "detailed_type": "movie", "tags": ["drama"],
            "duration_seconds": 5400, "catalog_url": URL.format(id="100003")}], "classics")
        return db

    def _record(self, db, video_id):
        r = check(video_id)
        registry.record_verification(
            db, video_id, r.status, r.final_url, failure_reason=r.failure_reason)
        return registry.get(db, video_id)

    def test_repeated_transient_errors_never_make_a_title_unavailable(self, db):
        for _ in range(registry.UNAVAILABLE_THRESHOLD + 2):
            row = self._record(db, "100018")
        assert row["verification_status"] == "transient_error"
        assert row["consecutive_failures"] == 0

    def test_definitive_failures_make_a_title_unavailable_at_the_threshold(self, db):
        for _ in range(registry.UNAVAILABLE_THRESHOLD):
            row = self._record(db, "100003")
        assert row["verification_status"] == "unavailable"
        assert row["failure_reason"] == "http_404"
