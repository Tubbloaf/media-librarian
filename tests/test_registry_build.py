"""Tests for registry_build.py's category harvest.

The failure this file exists for: cmd_enrich_categories() raised
NameError on an undefined delay constant after its very first slug, on
every run from 2026-08-14 to 2026-08-20. Nothing tested more than one
slug, and a dead harvest looked exactly like a finished one.
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import registry
import registry_build as build
import source


@pytest.fixture()
def harvest(tmp_path, monkeypatch):
    db = tmp_path / "registry.db"
    registry.init(db)
    monkeypatch.setattr(build, "DB_PATH", db)
    monkeypatch.setattr(build, "CATEGORY_REQUEST_DELAY", 0)
    return db


def _args(**kw):
    return types.SimpleNamespace(all_categories=False, **kw)


def _item(video_id, slug):
    return {
        "video_id": video_id, "detailed_type": "movie", "title": f"T{video_id}",
        "year": 2000, "duration_seconds": 5400, "tags": [slug], "actors": [],
        "url": f"https://catalog.example.test/movies/{video_id}/m",
    }


class TestHarvestCoversEverySlug:
    def test_all_slugs_are_fetched_not_just_the_first(self, harvest,
                                                      monkeypatch):
        seen = []
        monkeypatch.setattr(build, "_slugs_from_profile",
                            lambda: ["a", "b", "c", "d"])
        monkeypatch.setattr(source, "search_category",
                            lambda slug, **k: (seen.append(slug),
                                               [_item(f"{slug}1", slug)])[1])
        assert build.cmd_enrich_categories(_args()) == 0
        assert seen == ["a", "b", "c", "d"]

    def test_every_slug_reaches_the_registry(self, harvest, monkeypatch):
        monkeypatch.setattr(build, "_slugs_from_profile", lambda: ["a", "b"])
        monkeypatch.setattr(source, "search_category",
                            lambda slug, **k: [_item(f"{slug}1", slug)])
        build.cmd_enrich_categories(_args())
        for slug in ("a", "b"):
            assert registry.get(harvest, f"{slug}1") is not None


class TestPartialHarvestIsLoud:
    def _flaky(self, bad):
        def fetch(slug, **k):
            if slug == bad:
                raise NameError("REQUEST_DELAY_SECONDS")
            return [_item(f"{slug}1", slug)]
        return fetch

    def test_one_bad_slug_does_not_stop_the_rest(self, harvest, monkeypatch):
        monkeypatch.setattr(build, "_slugs_from_profile",
                            lambda: ["a", "bad", "c"])
        monkeypatch.setattr(source, "search_category", self._flaky("bad"))
        build.cmd_enrich_categories(_args())
        assert registry.get(harvest, "c1") is not None

    def test_partial_harvest_exits_non_zero(self, harvest, monkeypatch):
        monkeypatch.setattr(build, "_slugs_from_profile", lambda: ["a", "bad"])
        monkeypatch.setattr(source, "search_category", self._flaky("bad"))
        assert build.cmd_enrich_categories(_args()) == 1

    def test_clean_harvest_exits_zero(self, harvest, monkeypatch):
        monkeypatch.setattr(build, "_slugs_from_profile", lambda: ["a", "b"])
        monkeypatch.setattr(source, "search_category",
                            lambda slug, **k: [_item(f"{slug}1", slug)])
        assert build.cmd_enrich_categories(_args()) == 0

    def test_summary_counts_attempted_succeeded_failed(self, harvest,
                                                       monkeypatch, capsys):
        monkeypatch.setattr(build, "_slugs_from_profile",
                            lambda: ["a", "bad", "c"])
        monkeypatch.setattr(source, "search_category", self._flaky("bad"))
        build.cmd_enrich_categories(_args())
        out = capsys.readouterr().out
        assert "attempted=3 succeeded=2 failed=1" in out

    def test_failure_is_prominent_and_names_the_slug(self, harvest,
                                                     monkeypatch, capsys):
        monkeypatch.setattr(build, "_slugs_from_profile", lambda: ["a", "bad"])
        monkeypatch.setattr(source, "search_category", self._flaky("bad"))
        build.cmd_enrich_categories(_args())
        out = capsys.readouterr().out
        assert "PARTIAL HARVEST" in out
        assert "bad: NameError" in out

    def test_a_nameerror_is_caught_like_any_other_failure(self, harvest,
                                                          monkeypatch):
        # The original bug was a NameError, not anything network-shaped.
        monkeypatch.setattr(build, "_slugs_from_profile", lambda: ["bad"])
        monkeypatch.setattr(source, "search_category", self._flaky("bad"))
        assert build.cmd_enrich_categories(_args()) == 1
