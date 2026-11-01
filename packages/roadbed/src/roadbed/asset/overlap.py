"""Train/holdout disjointness: exact (row hash, input content address) and near (word-shingle Jaccard >= 0.8).

Near overlap is measured on the raw inputs, which live in the bucket by content address (oid = input_ref without
`sha256:`), never in the asset. With both sides' raw inputs the Jaccard is exact (an inverted shingle index finds
every candidate pair). With only the holdout MinHash object (SEAL.json minhash_oid) and the training inputs, it is
the MinHash estimate. Without the training inputs it cannot be measured, and the caller says so.

MinHash: slot j of a signature is the min over shingles s of the first 8 bytes of sha256("<j>|<s>"), as 16 hex.
The holdout object is JCS({"num_perm", "shingle", "signatures": {row_hash: [hex, ...]}}).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from roadbed.asset.hashing import jcs

DEFAULT_THRESHOLD, DEFAULT_NUM_PERM, DEFAULT_SHINGLE = 0.8, 128, 3


def shingles(text: str, k: int = DEFAULT_SHINGLE) -> frozenset[str]:
    """Word k-shingles. A text shorter than k words is one shingle (itself), so it still compares."""
    w = text.split()
    if len(w) < k:
        return frozenset([" ".join(w)]) if w else frozenset()
    return frozenset(" ".join(w[i:i + k]) for i in range(len(w) - k + 1))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    u = len(a | b)
    return len(a & b) / u if u else 0.0


def minhash(sh: Iterable[str], num_perm: int = DEFAULT_NUM_PERM) -> list[str]:
    s = list(sh)
    if not s:
        return ["f" * 16] * num_perm
    return [min(hashlib.sha256(f"{j}|{x}".encode()).digest()[:8] for x in s).hex() for j in range(num_perm)]


def minhash_object(signatures: Mapping[str, list[str]], num_perm: int = DEFAULT_NUM_PERM, shingle: int = DEFAULT_SHINGLE) -> bytes:
    """Bytes of the holdout MinHash object; its oid is their sha256."""
    return jcs({"num_perm": num_perm, "shingle": shingle, "signatures": dict(signatures)})


def parse_minhash_object(data: bytes) -> tuple[int, int, dict[str, list[str]]]:
    obj = json.loads(data)
    if not isinstance(obj, dict) or set(obj) != {"num_perm", "shingle", "signatures"}:
        raise ValueError("MinHash object must have exactly num_perm, shingle and signatures")
    n, k, sigs = obj["num_perm"], obj["shingle"], obj["signatures"]
    if not (isinstance(n, int) and isinstance(k, int) and n > 0 and k > 0 and isinstance(sigs, dict)):
        raise ValueError("MinHash object: bad num_perm, shingle or signatures")
    for h, sig in sigs.items():
        if not isinstance(sig, list) or len(sig) != n or not all(isinstance(x, str) and len(x) == 16 for x in sig):
            raise ValueError(f"MinHash object: signature of {h} is not {n} 16-hex slots")
    return n, k, sigs


def _sig_array(sigs: list[list[str]]) -> np.ndarray:
    return np.array([[int(x, 16) for x in s] for s in sigs], dtype=np.uint64)


@dataclass
class NearPair:
    holdout: str
    train: str
    jaccard: float


@dataclass
class Overlap:
    """Result of a disjointness check. `near_method` is None when near overlap could not be measured."""

    exact_row_hashes: list[str] = field(default_factory=list)
    shared_inputs: list[str] = field(default_factory=list)  # input_ref values present on both sides
    near_method: str | None = None
    near_pairs: list[NearPair] = field(default_factory=list)
    max_jaccard: float = 0.0
    near_note: str = ""

    @property
    def clean(self) -> bool:
        return not self.exact_row_hashes and not self.shared_inputs and not self.near_pairs


def exact_overlap(train: list[dict[str, Any]], holdout: list[dict[str, Any]], ov: Overlap) -> None:
    """Shared row hashes, and shared input content addresses (same input text under another row hash)."""
    th = {r["row_hash"] for r in train}
    ov.exact_row_hashes = sorted(th & {r["row_hash"] for r in holdout})
    ti = {r["input_ref"] for r in train}
    ov.shared_inputs = sorted(ti & {r["input_ref"] for r in holdout})


def near_exact(
    train: list[tuple[str, str]], holdout: list[tuple[str, str]], threshold: float, shingle: int, ov: Overlap
) -> None:
    """Exact Jaccard over (row_hash, text) pairs on both sides. Every pair sharing a shingle is compared."""
    hs = [(h, shingles(t, shingle)) for h, t in holdout]
    index: dict[str, list[int]] = defaultdict(list)
    for i, (_, s) in enumerate(hs):
        for x in s:
            index[x].append(i)
    best = 0.0
    pairs = []
    for th, text in train:
        ts = shingles(text, shingle)
        cand = {i for x in ts for i in index.get(x, ())}
        for i in cand:
            j = jaccard(ts, hs[i][1])
            best = max(best, j)
            if j >= threshold:
                pairs.append(NearPair(holdout=hs[i][0], train=th, jaccard=round(j, 6)))
    ov.near_method, ov.max_jaccard = "jaccard", round(best, 6)
    ov.near_pairs = sorted(pairs, key=lambda p: (p.holdout, p.train))


def near_minhash(
    train: list[tuple[str, str]], holdout_sigs: Mapping[str, list[str]], threshold: float, num_perm: int, shingle: int,
    ov: Overlap,
) -> None:
    """MinHash estimate: the share of equal slots between each training row and each holdout signature."""
    keys = sorted(holdout_sigs)
    if not keys or not train:
        ov.near_method, ov.max_jaccard = "minhash", 0.0
        return
    hsig = _sig_array([holdout_sigs[k] for k in keys])
    tsig = _sig_array([minhash(shingles(t, shingle), num_perm) for _, t in train])
    best = 0.0
    pairs = []
    for start in range(0, len(tsig), 256):
        est = (tsig[start:start + 256, None, :] == hsig[None, :, :]).mean(axis=2)
        best = max(best, float(est.max()))
        for ti, hi in zip(*np.nonzero(est >= threshold)):
            pairs.append(NearPair(holdout=keys[hi], train=train[start + ti][0], jaccard=round(float(est[ti, hi]), 6)))
    ov.near_method, ov.max_jaccard = "minhash", round(best, 6)
    ov.near_pairs = sorted(pairs, key=lambda p: (p.holdout, p.train))
