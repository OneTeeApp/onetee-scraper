"""Deterministic course sharding for horizontal scale.

At ~15k US courses no single CI job can scrape everything within a run window,
so work is split across N parallel jobs. `--shard i/N` selects a stable 1/N
slice of the courses (by sorted slug, modulo N) so:
  * every course is covered by exactly one shard,
  * the split is identical across dates and reruns (a course always lands in
    the same shard — stable caching / debugging), and
  * shards are balanced regardless of platform ordering.

Rate-limited shared hosts (e.g. kenna for TeeItUp) need each shard to pace at
its fair fraction of the global budget; adapters read SHARD_COUNT for that.
"""
from __future__ import annotations

import os


def parse_shard(spec: str | None) -> tuple[int, int]:
    """'i/N' -> (i, N); None/'' -> (0, 1) meaning 'all courses, one shard'."""
    if not spec:
        return 0, 1
    i_str, n_str = spec.split("/")
    i, n = int(i_str), int(n_str)
    if not (n >= 1 and 0 <= i < n):
        raise ValueError(f"bad --shard {spec!r}: need 0 <= i < N and N >= 1")
    return i, n


def apply_only_courses(courses: list[dict]) -> list[dict]:
    """Narrow to the slugs named in the ONLY_COURSES env var, if it is set.

    WHY AN ENV VAR AND NOT A --courses FLAG ON EVERY SCRAPER. Booking-window
    pruning is per-COURSE, not per-platform: scripts/derive_windows.py records
    {slug: {max_offset_with_rows, checked_through}} for every course
    individually, and scripts/eligible_courses.py answers "which slugs are worth
    fetching for THIS date". aggregate.py already consumes that through its
    --courses flag; the six browser scrapers had no equivalent, which is the
    only reason they were never pruned. Every one of them narrows its course
    list through apply_shard() immediately after loading the registry, so this
    is the single chokepoint that reaches all of them at once — no six-way
    change to modules that each parse their own arguments.

    Contract, matching eligible_courses.py's own output:
      * unset, empty, or "ALL"  -> no filtering
      * a comma-separated slug list -> intersect with it
      * slugs that this platform does not have are ignored

    FAILS OPEN. If the intersection is empty the ORIGINAL list is returned, not
    an empty one. "No eligible courses for this date" is a legitimate answer,
    but eligible_courses.py signals it with NONE and the caller skips the date
    before ever invoking a scraper — so an empty intersection here means the
    slug list and the registry disagree, and scraping everything is the safe
    direction. Landing zero rows would look to d1.sync like a platform-wide
    empty and to the operator like a working run.
    """
    raw = os.environ.get("ONLY_COURSES", "").strip()
    if not raw or raw == "ALL":
        return courses
    wanted = {s.strip() for s in raw.split(",") if s.strip()}
    kept = [c for c in courses if c.get("slug") in wanted]
    if not kept:
        print(f"[sharding] ONLY_COURSES matched none of {len(courses)} courses "
              f"— ignoring the filter and scraping all of them")
        return courses
    print(f"[sharding] ONLY_COURSES: {len(kept)}/{len(courses)} courses")
    return kept


def apply_shard(courses: list[dict], spec: str | None) -> list[dict]:
    """Return this shard's slice of `courses`, deterministic by sorted slug.

    Also applies the ONLY_COURSES narrowing (see above) FIRST, so a pruned run
    shards whatever survived pruning rather than sharding the full list and
    then throwing most of each slice away.
    """
    courses = apply_only_courses(courses)
    i, n = parse_shard(spec)
    if n == 1:
        return courses
    ordered = sorted(courses, key=lambda c: c["slug"])
    return [c for idx, c in enumerate(ordered) if idx % n == i]


def shard_count(spec: str | None) -> int:
    """N from an 'i/N' spec (1 if unsharded). Adapters divide per-host rate
    budgets by this so all shards together stay under the limit."""
    return parse_shard(spec)[1]


def set_env_shard_count(spec: str | None) -> None:
    """Publish the shard count so per-host throttles (imported anywhere) can
    scale their cadence to 1/N without threading the value through every call."""
    os.environ["SHARD_COUNT"] = str(shard_count(spec))
