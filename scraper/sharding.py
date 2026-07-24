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


def apply_shard(courses: list[dict], spec: str | None) -> list[dict]:
    """Return this shard's slice of `courses`, deterministic by sorted slug."""
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
