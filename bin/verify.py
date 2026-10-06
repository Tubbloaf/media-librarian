"""Playability verifier: deterministic, fixture-backed.

verify_playable(video_id, url) is the single authoritative check the
pipeline calls before launching a movie. The private system fetches the
title's page and confirms the id is actively served. Here the outcome comes
from the fixture catalog's declared playback state (catalog.playback()), and
the result policy is unchanged:

VerifyResult.status values:
    'live'            -- playable
    'failed'          -- definitive failure; counts toward consecutive_failures
                         in the registry (3 in a row -> 'unavailable')
    'transient_error' -- connectivity/timeout problem; does NOT count

VerifyResult.failure_reason tags (when status != 'live'):
    'http_404'           -- the page does not exist
    'http_error'         -- other HTTP failure
    'login_redirect'     -- the page sits behind a login wall
    'parse_error'        -- the page loaded but its data is unusable
    'id_not_in_response' -- the page loaded but does not list this id
    'transient_error'    -- connection refused, timeout, DNS failure

A 'live' result may also carry availability_starts / availability_ends so
the caller can store a fresh availability window.

'invalid_record' (a row with no URL) is decided by the caller, not here: a
data-quality problem is not evidence about the title.
"""
from __future__ import annotations

from dataclasses import dataclass

import catalog


@dataclass
class VerifyResult:
    status: str                          # 'live' | 'failed' | 'transient_error'
    failure_reason: str | None = None
    final_url: str | None = None         # URL actually reached
    availability_starts: str | None = None
    availability_ends: str | None = None


# fixture playback state -> (status, failure_reason)
_FAILURES = {
    "not_found":          ("failed", "http_404"),
    "http_error":         ("failed", "http_error"),
    "login_redirect":     ("failed", "login_redirect"),
    "parse_error":        ("failed", "parse_error"),
    "id_not_in_response": ("failed", "id_not_in_response"),
    "transient_error":    ("transient_error", "transient_error"),
}


def verify_playable(
    video_id: str,
    url: str,
    content_type: str = "movie",
    timeout: int = 10,
) -> VerifyResult:
    """Report whether video_id is playable at url, per the fixture catalog.

    content_type is accepted for series support and does not change the
    check. An id the catalog does not list is 'id_not_in_response'."""
    behavior = catalog.playback(video_id)
    if behavior is None:
        return VerifyResult("failed", "id_not_in_response", final_url=url)
    state = behavior.get("state", "live")
    if state == "live":
        return VerifyResult(
            "live", final_url=url,
            availability_starts=behavior.get("availability_starts"),
            availability_ends=behavior.get("availability_ends"),
        )
    if state == "login_redirect":
        return VerifyResult("failed", "login_redirect",
                            final_url="https://catalog.example.test/login")
    status, reason = _FAILURES[state]
    return VerifyResult(status, reason, final_url=url)
