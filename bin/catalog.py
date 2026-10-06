"""Fixture-backed catalog: the public stand-in for live acquisition.

The private system fetches category grids, show pages, sitemaps and
playability checks over HTTP. None of that ships here. Instead
`fixtures/catalog.json` -- hand-authored, fictional titles -- plays the part
of the outside world, and `source`, `sitemap` and `verify`
read from it through this module with the same function signatures and the
same failure taxonomy the live versions have.

Fixture layout (see fixtures/catalog.json):

    categories   slug -> [content id, ...]            what a category grid lists
    titles       content id -> catalog entry          the per-title record
    episodes     series id -> [{episode_id, title}]   what a show page lists
    page_state   id -> failure kind                   default 'ok'
    playback     id -> {state, availability_*}        default 'live'
    sitemap      [{video_id, title, ...}]             sitemap-shaped rows
    sitemap_files_failed                              simulated partial import

Point the module at another catalog with `use(path)` or the CATALOG_PATH
environment variable; the default is the file shipped in this repository.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "catalog.json"

_path: Path | None = None
_cache: dict[str, dict] = {}


def use(path: Path | str | None) -> None:
    """Select the catalog file; None restores the default."""
    global _path
    _path = Path(path) if path else None


def _resolve_path() -> Path:
    if _path is not None:
        return _path
    env = os.environ.get("CATALOG_PATH")
    return Path(env) if env else DEFAULT_PATH


def load() -> dict:
    p = _resolve_path()
    key = str(p)
    if key not in _cache:
        with open(p) as f:
            _cache[key] = json.load(f)
    return _cache[key]


def categories() -> list[str]:
    return list(load()["categories"])


def category_children(slug: str) -> list[str]:
    return list(load()["categories"].get(slug, []))


def entry(content_id: str) -> dict | None:
    return load()["titles"].get(content_id)


def episodes(series_id: str) -> list[dict]:
    return list(load().get("episodes", {}).get(series_id, []))


def page_state(content_id: str) -> str:
    return load().get("page_state", {}).get(content_id, "ok")


def playback(content_id: str) -> dict | None:
    """Declared playback behavior for an id, or None if the id is not in the
    catalog at all (the equivalent of a page that does not list it)."""
    pb = load().get("playback", {})
    if content_id in pb:
        return pb[content_id]
    return {"state": "live"} if content_id in load()["titles"] else None


def sitemap() -> tuple[list[dict], int]:
    data = load()
    return [dict(m) for m in data.get("sitemap", [])], data.get("sitemap_files_failed", 0)


def find_id(url_id: str) -> str | None:
    """Map a URL's id segment to a catalog id. Series URLs drop a leading
    zero that the catalog id keeps, so compare with zeros stripped."""
    titles = load()["titles"]
    if url_id in titles:
        return url_id
    stripped = url_id.lstrip("0")
    for cid in titles:
        if cid.lstrip("0") == stripped:
            return cid
    return None
