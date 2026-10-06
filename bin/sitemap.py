"""Sitemap source: rows shaped like a movie sitemap import.

The private system downloads and parses the catalog's sitemap files. Here the
same rows come from the fixture catalog (see catalog.py). Each row carries
video_id, title, description, duration_seconds, catalog_url, thumbnail_url and
sitemap_lastmod -- exactly what registry.register_from_sitemap() takes.
"""
from __future__ import annotations

import catalog


def fetch_all_movies(timeout: int = 30) -> tuple[list[dict], int]:
    """Return (movies, n_failed).

    n_failed is the number of sitemap files that could not be read; callers
    that need a complete import must check that it is 0 (the registry only
    writes its baseline marker after a complete import)."""
    movies, n_failed = catalog.sitemap()
    print(f"sitemap: {len(movies)} movies" + (f" ({n_failed} failed)" if n_failed else ""))
    return movies, n_failed
