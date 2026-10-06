"""Score and pick a video from source results against a taste profile.

Ranking is score() alone -- the highest weighted match wins.

Anything hitting an excluded tag or category is dropped before scoring.

Dayparting (2026-07-27) is a SOFT nudge on score(), not a filter -- nothing
is ever excluded for being "wrong for the hour". See _daypart_bonus().

preferred_tags and daypart tag_bonus are AVERAGED across a video's matching
tags, not summed (changed 2026-07-27 -- see _avg_matching_weight()).
Summing let a title win purely by carrying MORE tags than its competition,
regardless of whether those tags were a better thematic fit -- confirmed
live: "Everything Everywhere All at Once" (5 genre tags) out-scored
genuinely better-fitting single-window content (Transformers at 6-9am,
The Twilight Zone/Dark City at 11am-2pm) purely off tag breadth, not fit.
Averaging means a title's score reflects how well it fits on average,
not how many boxes it happens to check.

Per-tag IMDb TARGETS (2026-07-27, imdb_target_by_tag in the profile) are a
separate IMDb mechanism. This one wants a SPECIFIC number for a genre, not "as
high as possible" -- asymmetric on purpose: undershooting the target is a real
penalty, overshooting it is only ever a bonus, never a penalty. See
_imdb_target_bonus(). NOT part of score() (moved out 2026-07-27, see that
function's docstring) -- it's a tie-break step in pick() instead, so it
can only ever differentiate a rated title from another rated title, never
give a rated title a structural edge over an unrated one on the primary
score.
"""
import json
import random
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Dayparts are defined in Eastern Time explicitly (2026-07-27 user spec),
# NOT the system's local time zone -- confirmed the dev machine happens to
# already be on EDT, but that's a coincidence not worth depending on if
# this ever runs somewhere/somewhen else. zoneinfo also handles the
# EST/EDT daylight-saving transition correctly, unlike a fixed UTC offset.
DAYPART_TZ = ZoneInfo("America/New_York")

DEFAULT_PROFILE_PATH = Path(__file__).resolve().parent.parent / "taste_profile.json"


def load_profile(path: Path = DEFAULT_PROFILE_PATH) -> dict:
    with open(path) as f:
        return json.load(f)


def _excluded(video: dict, profile: dict) -> bool:
    excluded_tags = set(profile.get("excluded_tags", []))
    excluded_categories = set(profile.get("excluded_categories", []))
    if excluded_tags & set(video.get("tags", [])):
        return True
    if excluded_categories & set(video.get("categories", [])):
        return True
    # AND-condition hard filter (2026-07-27) -- separate from the two
    # single-tag checks above (which exclude on ANY one matching tag).
    # Added specifically so an exclusion like animation+mature is a real
    # content-level guarantee (a title carrying both tags is excluded no
    # matter which category it surfaced from), not just "don't source from
    # one category page". See the profile's excluded_tag_combinations.
    video_tags = set(video.get("tags", []))
    for combo in profile.get("excluded_tag_combinations", []):
        if set(combo) <= video_tags:
            return True
    return False



def _avg_matching_weight(weights: dict, items: list[str]) -> float:
    """Average, not sum, across whichever of `items` have a nonzero entry in
    `weights` (used for preferred_tags and a daypart window's tag_bonus --
    NOT preferred_stars/preferred_categories, which keep summing; multiple
    preferred actors in one title is a genuine deliberate positive, not the
    tag-breadth artifact this fixes).

    Added 2026-07-27 after confirming live that summing let a title win
    purely by carrying MORE weighted tags than its competition, regardless
    of fit -- a 5-tag title beat genuinely better-fitting 3-tag titles in
    two different daypart windows, purely off tag count (see module
    docstring). A title's contribution is now its best-fitting AVERAGE
    match, not (single match's worth) x (how many tags it happens to
    carry) -- same fix shape as _narrow_by_imdb_target_bonus(), which
    solved the identical breadth-stacking problem for IMDb ratings the
    same day.

    Division is over the MATCHING subset only, never every tag/item a
    video carries -- so extra tags outside weights (irrelevant to this
    mechanism) never dilute a title's real matches, same as
    _imdb_target_bonus()'s per-tag scoring elsewhere. Zero matches
    contributes exactly 0 -- same neutral-on-no-match stance as
    everywhere else in this file."""
    matched = [weights[item] for item in items if weights.get(item, 0) != 0]
    if not matched:
        return 0.0
    return sum(matched) / len(matched)


def current_hour() -> int:
    """The hour every daypart decision in one request should share.

    Window selection and scoring each used to read the clock for
    themselves. They agree almost always, and disagree exactly when a
    request straddles an hour boundary -- at which point a title is chosen
    from one window's category_slugs and then scored under the next
    window's tag_bonus, short_max and content_type_bonus. Capture this once
    at the start of a request and thread it through instead."""
    return datetime.now(DAYPART_TZ).hour


def _in_daypart_window(hour: int, window: dict) -> bool:
    min_hour, max_hour = window["min_hour"], window["max_hour"]
    if min_hour <= max_hour:
        return min_hour <= hour < max_hour
    return hour >= min_hour or hour < max_hour  # wraps past midnight, e.g. 22-4


def get_current_daypart_window(profile: dict, hour: int | None = None) -> dict | None:
    """Returns the full daypart window dict (not just its tag_bonus) matching
    `hour` -- added 2026-07-27 alongside category_slugs, since callers
    need the SAME hour-window lookup _daypart_bonus() already does, but to
    read category_slugs rather than tag_bonus. Reuses _in_daypart_window() so
    the two lookups can never disagree about which window is "current".

    Windows aren't expected to overlap in practice (unlike _daypart_bonus(),
    which deliberately stacks every matching window's tag_bonus), so this
    returns the first match rather than merging -- there is exactly one
    "current" window's category_slugs to source from at any given hour."""
    if hour is None:
        hour = current_hour()
    for window in profile.get("dayparts", []):
        if _in_daypart_window(hour, window):
            return window
    return None


def _daypart_bonus(video: dict, profile: dict, hour: int | None = None) -> float:
    """Soft nudge only -- opt-in, additive, never excludes anything (see
    module docstring and the occam pass 2026-07-27 that killed the
    hard-filter/genre-envelope version of this: this is a single person's
    viewing, not a broadcast slot with an unknown audience to guess at,
    and a hard filter on top of the existing exclusion filter risked
    emptying already-thin candidate pools further).

    Hour-of-day only -- no weekday/weekend modulation, kept out for the
    same reason (stripped to the minimal version worth building, not the
    full broadcast-dayparting model). `hour` defaults to the real current
    hour; only overridden for testing.

    Dayparts are windows in profile["dayparts"], each
    {min_hour, max_hour, tag_bonus: {tag: +/-N}}. Hours not covered by any
    window get no adjustment at all -- undefined = neutral, not zero-as-penalty. If more
    than one window matches the current hour, their bonuses stack (kept
    simple rather than picking "the" active one -- not expected to matter
    at this scope).

    Each window's tag_bonus contribution is AVERAGED across the video's
    matching tags, not summed -- see _avg_matching_weight(), changed
    2026-07-27 to stop a broadly-tagged title from out-scoring a
    genuinely better-fitting, narrowly-tagged one just by carrying more
    tags this window happens to boost.

    A window can also optionally set short_max_minutes/short_bonus --
    added once, flat, if the video's duration is under that threshold
    (not scaled by how much shorter). Added 2026-07-27 for the 9-11am
    window's "shorter, more TV-length" requirement -- the only window
    that cares about runtime at all, so this stays optional/per-window
    rather than a whole new global mechanism.

    content_type_bonus is the same idea keyed on content_type, added
    2026-08-20 once series became eligible: {"series": 3}. It sits
    alongside short_bonus rather than replacing it, because the two
    reach different content. The 9-11am window wants TV-*length*
    viewing, and a 50-minute movie satisfies that as well as an episode
    does -- so short_bonus is not an obsolete proxy for "is this TV", it
    is the mechanism for short movies, and series simply never trip it
    because they carry no runtime of their own. Both bonuses can apply
    to the same window; neither implies the other."""
    if hour is None:
        hour = current_hour()
    total = 0.0
    for window in profile.get("dayparts", []):
        if _in_daypart_window(hour, window):
            tag_bonus = window.get("tag_bonus", {})
            total += _avg_matching_weight(tag_bonus, video.get("tags", []))
            short_max = window.get("short_max_minutes")
            duration_seconds = video.get("duration_seconds")
            if short_max is not None and duration_seconds:
                if duration_seconds / 60 <= short_max:
                    total += window.get("short_bonus", 0)
            # Reads the stored type only -- no episode data, no fetch. A
            # series' runtime is unknowable without resolving it, and
            # resolution happens once on the final pick, long after scoring.
            content_type_bonus = window.get("content_type_bonus", {})
            total += content_type_bonus.get(
                video.get("detailed_type"), 0)
    return total


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _imdb_target_bonus(video: dict, profile: dict) -> float:
    """A tie-break signal (NOT part of score() -- see _narrow_by_imdb_target_bonus()
    and score()'s docstring), separate mechanism from _narrow_by_metric()
    (a tie-break preference for higher-is-better). This one is "I want
    something close to a specific
    number for this genre, not just as high as possible" (2026-07-27 user
    spec) -- e.g. wanting horror around 6.5, not the most
    critically-acclaimed horror available.

    Originally summed directly into score() -- moved out 2026-07-27 after
    confirming live that doing so gave any title with *any* imdb_rating a
    structural edge over every unrated title in the same candidate pool,
    regardless of real taste-profile fit (an unrated title can never earn
    this bonus, so baking it into the primary score wasn't rewarding
    "closer to this genre's ideal rating" so much as "happens to have
    rating data at all" -- confirmed live: a 4-tag rated title outscored a
    5-tag unrated title that actually matched more of the window's own
    tag_bonus, purely off this channel). Now only ever invoked by
    _narrow_by_imdb_target_bonus() to compare rated titles to each other.

    Per profile["imdb_target_by_tag"][tag] = {target, spread, max_bonus}:
    ASYMMETRIC by explicit user request, not a symmetric "distance from
    target" curve -- above target, bonus ramps linearly from 0 (at
    target) up to +max_bonus (at target+spread) and stays capped at
    max_bonus beyond that, never penalized for exceeding the target.
    Below target, it ramps the same way but NEGATIVE (down to -max_bonus
    at target-spread, capped beyond) -- undershooting the target
    genuinely hurts, overshooting never does.

    A tag's spec can also be {"mode": "maximize", "multiplier": M} instead
    of a target -- added 2026-07-27 for tags the user wants "no specific
    number, just always prefer whichever is highest-rated" (e.g. science
    & nature) rather than a target to hit. Uncapped and never negative:
    bonus = rating * multiplier, straight proportional reward, no ramp,
    no ceiling. Different in kind from the target mode above (which is
    capped and penalizes undershooting) -- this mode has no notion of
    "too low", it just always rewards higher more.

    Sums across every tag the video carries that has a spec defined
    (same additive-across-tags convention as preferred_tags/daypart
    tag_bonus elsewhere in score()).
    Missing imdb_rating, or no imdb_target_by_tag entries at all, both
    contribute exactly 0 -- neutral, same stance as everywhere else this
    project touches imdb_rating."""
    targets = profile.get("imdb_target_by_tag", {})
    if not targets:
        return 0.0
    rating = _as_float(video.get("imdb_rating"))
    if rating is None:
        return 0.0
    total = 0.0
    for tag in video.get("tags", []):
        spec = targets.get(tag)
        if not spec:
            continue
        if spec.get("mode") == "maximize":
            total += rating * spec.get("multiplier", 0.5)
            continue
        target = spec["target"]
        spread = spec.get("spread", 1.0)
        max_bonus = spec.get("max_bonus", 3.0)
        delta = rating - target
        if spread <= 0:
            magnitude = max_bonus if delta != 0 else 0.0
        else:
            magnitude = max_bonus * min(abs(delta) / spread, 1.0)
        total += magnitude if delta >= 0 else -magnitude
    return total


def _narrow_by_metric(group: list[dict], metric_key: str) -> list[dict]:
    """Among `group`, prefer whichever have a real numeric value for
    metric_key, then narrow further to the max value among those. If NONE
    have a value, the group is unchanged (unrated titles are never
    disqualified, just don't get to use this metric to win a tie -- same
    neutral-on-missing-data stance."""
    rated = [(v, _as_float(v.get(metric_key))) for v in group]
    rated = [(v, r) for v, r in rated if r is not None]
    if not rated:
        return group
    best = max(r for _, r in rated)
    return [v for v, r in rated if r == best]


def _narrow_by_imdb_target_bonus(group: list[dict], profile: dict) -> list[dict]:
    """Tie-break step (2026-07-27) for _imdb_target_bonus() -- narrows to the
    highest imdb_target_bonus among whichever candidates in `group` HAVE an
    imdb_rating at all. Gated on the raw rating's PRESENCE
    (video.get("imdb_rating") is not None), not on the computed bonus being
    nonzero -- a rated title could legitimately compute to exactly 0 if its
    rating sits precisely at a tag's target, and that's still "has a
    rating" for this purpose. If none of the group has a rating, unchanged
    -- same neutral-on-missing-data pattern as _narrow_by_metric(), and by
    design (see _imdb_target_bonus()'s docstring): this must never let a
    rated title's fit be compared against an unrated title's absence of
    one, only against another rated title's fit."""
    rated = [(v, _imdb_target_bonus(v, profile)) for v in group if v.get("imdb_rating") is not None]
    if not rated:
        return group
    best = max(b for _, b in rated)
    return [v for v, b in rated if b == best]


def score(video: dict, profile: dict, hour: int | None = None) -> float:
    """preferred_tags and the current hour's daypart tag_bonus are AVERAGED
    across a video's matching tags (see _avg_matching_weight(), changed
    2026-07-27) -- a title doesn't win just by carrying more boosted tags
    than its competition, it wins by fitting them well on average.
    preferred_categories/preferred_stars still SUM (categories is
    currently always empty in practice -- see taste_profile.json's
    comment -- and multiple preferred actors in one title is a genuine
    deliberate positive, not the tag-breadth artifact this fixes for
    tags).

    Deliberately does NOT include _imdb_target_bonus() (moved out
    2026-07-27, see that function's docstring and
    _narrow_by_imdb_target_bonus()) -- an IMDb rating must never give a
    title a structural edge on the PRIMARY score over a title that simply
    has no rating at all (the catalog's coverage skews hard against niche/cult
    content). The target bonus still matters --
    see pick() -- just only as a tie-break among candidates that are
    already tied AND already rated."""
    tag_weights = profile.get("preferred_tags", {})
    category_weights = profile.get("preferred_categories", {})
    star_weights = profile.get("preferred_stars", {})
    total = 0.0
    total += _avg_matching_weight(tag_weights, video.get("tags", []))
    total += sum(category_weights.get(cat, 0) for cat in video.get("categories", []))
    total += sum(star_weights.get(star, 0) for star in video.get("stars", []))
    total += _daypart_bonus(video, profile, hour)
    return total


def pick(
    videos: list[dict],
    profile: dict,
    exclude_ids: set[str] | None = None,
    hour: int | None = None,
    feedback: dict[str, float] | None = None,
) -> dict | None:
    """Return the best non-excluded video, ranked by
    score() (see module docstring). Ties are broken in four steps, not
    straight to random (added 2026-07-27, after confirming live that most
    "0 base preferred_tags score" wins were actually tied groups of
    same-tagged titles -- e.g. every crime documentary getting an
    identical score -- where nothing was differentiating a genuinely
    better title from a worse one):
      1. Narrow to the highest imdb_target_bonus among the tied group,
         comparing only whichever candidates HAVE an imdb_rating at all
         to each other (added 2026-07-27, moved out of score() itself --
         see _narrow_by_imdb_target_bonus() and score()'s docstring for
         why this can't live in the primary score: it must never let a
         rated title beat an unrated one just for having rating data).
      2. If still tied, narrow to the highest imdb_rating among the tied
         group (titles with no rating at all are never excluded by this --
         they just don't win a tie-break on a metric they don't have; see
         _narrow_by_metric()).
      3. If still tied, narrow the same way by rotten_tomatoes_score.
      4. Whatever's left (genuinely equal, or all unrated) -- random,
         not score()'s incidental list order. Confirmed live 2026-07-27
         this matters: a real category pool had 10+ titles tied at the
         top score, and a plain max() always returns the first one in
         list order, every time, deterministically -- so even with
         exclude_ids correctly removing an already-played title, the
         *next* pick just locked onto whichever other tied title came
         first, not a genuinely different one. That was the actual cause
         of a title resurfacing repeatedly, not a flaw in the
         exclude_ids/persistence logic itself.

    exclude_ids: video_ids to skip (e.g. recently played; the caller owns
    that tracking) -- without this, source's identical
    query returns an identical pool every time and pick() is fully
    deterministic, so the same trigger always plays the same video. If
    excluding these would leave no candidates, falls back to ignoring the
    exclusion rather than returning None -- better to repeat than play
    nothing.

    hour: passed straight through to score() for the daypart bonus (see
    _daypart_bonus) -- defaults to the real current hour (Eastern). Only
    overridden for testing a specific time slot without waiting for it.

    feedback: optional {video_id: adjustment} added to a candidate's score
    (see feedback.py). Absent ids adjust by 0, so passing nothing -- or an
    empty dict -- leaves scoring exactly as it was.
    """
    candidates = [v for v in videos if not _excluded(v, profile)]
    if not candidates:
        return None
    if exclude_ids:
        fresh = [v for v in candidates if v["video_id"] not in exclude_ids]
        if fresh:
            candidates = fresh
    feedback = feedback or {}
    scored = [(score(v, profile, hour) + feedback.get(v["video_id"], 0.0), v)
              for v in candidates]
    best_score = max(s for s, _ in scored)
    top = [v for s, v in scored if s == best_score]
    top = _narrow_by_imdb_target_bonus(top, profile)
    top = _narrow_by_metric(top, "imdb_rating")
    top = _narrow_by_metric(top, "rotten_tomatoes_score")
    return random.choice(top)
