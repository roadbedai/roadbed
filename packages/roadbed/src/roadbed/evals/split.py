"""Stratified sampling: proportional allocation over the joint slice strata, balanced on every slice marginal.

Each stratum (one combination of slice values) first gets floor(fraction x size) rows. The remaining rows go one
at a time to the stratum whose slice values are furthest below their proportional target, summed over the slice
keys; no stratum gets more than ceil(fraction x size). So the total is exact and every key=value share stays
close to its share of the pool. Inside a stratum, rows are ranked by sha256("<seed>:<tag>:<interaction_id>"):
the choice depends on the seed and the ids only, never on input order or on labels.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

Stratum = tuple[str, ...]


def rank_key(seed: int, tag: str, interaction_id: str) -> str:
    return hashlib.sha256(f"{seed}:{tag}:{interaction_id}".encode()).hexdigest()


def stratum_of(row: Mapping[str, Any], keys: Sequence[str]) -> Stratum:
    return tuple(row["slices"][k] for k in keys)


def allocate(sizes: Mapping[Stratum, int], fraction: float, n_keys: int, seed: int, tag: str) -> dict[Stratum, int]:
    """Rows to draw per stratum. sum == round(fraction x total)."""
    total = sum(sizes.values())
    want = round(fraction * total)
    quota = {s: fraction * n for s, n in sizes.items()}
    alloc = {s: math.floor(q) for s, q in quota.items()}
    targets: list[dict[str, float]] = [defaultdict(float) for _ in range(n_keys)]
    current: list[dict[str, int]] = [defaultdict(int) for _ in range(n_keys)]
    for s, q in quota.items():
        for k in range(n_keys):
            targets[k][s[k]] += q
            current[k][s[k]] += alloc[s]
    tie = {s: rank_key(seed, tag, "|".join(s)) for s in sizes}
    for _ in range(want - sum(alloc.values())):
        open_ = [s for s in sizes if alloc[s] < math.ceil(quota[s]) and alloc[s] < sizes[s]]
        if not open_:
            break
        best = max(open_, key=lambda s: (
            round(sum(targets[k][s[k]] - current[k][s[k]] for k in range(n_keys)), 9), round(quota[s] - alloc[s], 9), tie[s]))
        alloc[best] += 1
        for k in range(n_keys):
            current[k][best[k]] += 1
    return alloc


def draw(rows: Sequence[Mapping[str, Any]], keys: Sequence[str], fraction: float, seed: int, tag: str) -> set[str]:
    """Interaction ids of a stratified sample of `rows` (proportional, see the module docstring)."""
    by: dict[Stratum, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        by[stratum_of(r, keys)].append(r)
    alloc = allocate({s: len(rs) for s, rs in by.items()}, fraction, len(keys), seed, tag)
    chosen: set[str] = set()
    for s, rs in by.items():
        ranked = sorted(rs, key=lambda r: rank_key(seed, tag, r["interaction_id"]))
        chosen.update(r["interaction_id"] for r in ranked[: alloc[s]])
    return chosen


def max_share_gap(sample: Sequence[Mapping[str, Any]], pool: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> float:
    """Largest |share in sample - share in pool| over every key=value, in share units (0.02 = 2 points)."""
    gap = 0.0
    if not sample or not pool:
        return gap
    for k in keys:
        cs: dict[str, int] = defaultdict(int)
        cp: dict[str, int] = defaultdict(int)
        for r in sample:
            cs[r["slices"][k]] += 1
        for r in pool:
            cp[r["slices"][k]] += 1
        for v in cp:
            gap = max(gap, abs(cs[v] / len(sample) - cp[v] / len(pool)))
    return gap
