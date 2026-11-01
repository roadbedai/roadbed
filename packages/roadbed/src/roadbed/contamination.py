"""Contamination: exact and near overlap between a training snapshot and the sealed holdout.

Exact overlap is a shared row hash. Near overlap compares the raw input texts: word shingles, then
MinHash (or exact Jaccard for small sets), flagged at Jaccard >= policy.contamination.minhash_threshold.
spec/METHOD.md explains the method in plain words; SPEC.md, "Seal precedes training", has the rules.

The seal order needs git, SEAL.json and the receipt, so the caller computes it and passes it in.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, Literal

import numpy as np

from roadbed.contracts import (
    SPEC,
    ContaminationPolicy,
    ContaminationReport,
    DatasetRef,
    ExactOverlap,
    GatePolicy,
    NearOverlap,
    NearPair,
    SealOrder,
)

Contamination = ContaminationReport

_DECIMALS = 6  # jaccard values in contamination.json are rounded to 6 decimals


def shingles(text: str, k: int) -> set[str]:
    """Word k-shingles of text (split on whitespace). A text with fewer than k words is one shingle."""
    words = text.split()
    if not words:
        return set()
    if len(words) < k:
        return {" ".join(words)}
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    """|a & b| / |a | b|; 0.0 when both are empty."""
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def signature(sh: set[str], num_perm: int) -> list[str]:
    """MinHash signature: slot j is the min over shingles of the first 8 bytes of sha256("<j>|<shingle>"), as 16 hex.

    This is the format of the holdout MinHash object that SEAL.json.minhash_oid names.
    """
    if not sh:
        raise ValueError("no shingles: an empty text has no MinHash signature")
    return [min(hashlib.sha256(f"{j}|{s}".encode()).digest()[:8] for s in sh).hex() for j in range(num_perm)]


def _matrix(sigs: list[list[str]], num_perm: int) -> np.ndarray:
    m = np.empty((len(sigs), num_perm), dtype=np.uint64)
    for r, sig in enumerate(sigs):
        m[r] = [int(x, 16) for x in sig]
    return m


def _texts(rows: Sequence[Mapping[str, Any]], texts: Mapping[str, str], label: str) -> list[str]:
    """The raw input text of each row, checked against its input_ref (sha256 of the text bytes)."""
    out = []
    for r in rows:
        ref = r.get("input_ref")
        if not isinstance(ref, str) or ref not in texts:
            raise ValueError(f"{label}: no text for input_ref {ref!r}")
        text = texts[ref]
        if "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest() != ref:
            raise ValueError(f"{label}: text for {ref} does not hash to its input_ref")
        out.append(text)
    return out


def _row_hashes(rows: Sequence[Mapping[str, Any]], label: str) -> list[str]:
    hashes = []
    for r in rows:
        h = r.get("row_hash")
        if not isinstance(h, str) or len(h) != 64:
            raise ValueError(f"{label}: every row needs its row_hash (row {r.get('id') or r.get('interaction_id')!r})")
        hashes.append(h)
    return hashes


def _near(hold: list[tuple[str, set[str]]], train: list[tuple[str, set[str]]], cfg: ContaminationPolicy,
          method: Literal["minhash", "jaccard"]) -> NearOverlap:
    hold = [(h, s) for h, s in hold if s]
    train = [(h, s) for h, s in train if s]
    pairs: list[NearPair] = []
    max_j = 0.0
    if method == "jaccard":
        for th, ts in train:
            for hh, hs in hold:
                x = jaccard(hs, ts)
                max_j = max(max_j, x)
                if x >= cfg.minhash_threshold:
                    pairs.append(NearPair(holdout=hh, train=th, jaccard=round(x, _DECIMALS)))
    elif hold and train:
        hm = _matrix([signature(s, cfg.num_perm) for _, s in hold], cfg.num_perm)
        tm = _matrix([signature(s, cfg.num_perm) for _, s in train], cfg.num_perm)
        for i, (hh, _) in enumerate(hold):
            est = (tm == hm[i]).mean(axis=1)  # share of equal slots estimates the Jaccard similarity
            max_j = max(max_j, float(est.max()))
            for j in np.flatnonzero(est >= cfg.minhash_threshold):
                pairs.append(NearPair(holdout=hh, train=train[j][0], jaccard=round(float(est[j]), _DECIMALS)))
    pairs.sort(key=lambda p: (p.holdout, p.train))
    return NearOverlap(method=method, num_perm=cfg.num_perm, threshold=cfg.minhash_threshold,
                       max_jaccard=round(max_j, _DECIMALS), pairs=pairs)


def check(
    train_rows: Sequence[Mapping[str, Any]],
    holdout_rows: Sequence[Mapping[str, Any]],
    policy: GatePolicy,
    *,
    texts: Mapping[str, str],
    rebase: int,
    holdout: DatasetRef,
    train: DatasetRef,
    seal_order: SealOrder,
    synthetic: bool,
    method: Literal["minhash", "jaccard"] = "minhash",
) -> ContaminationReport:
    """Build contamination.json for one rebase.

    train_rows are snapshot rows, holdout_rows are holdout rows; both carry row_hash and input_ref.
    texts maps input_ref to the raw (pseudonymised) input text; each text must hash to its input_ref.
    holdout and train are the dataset refs (id, Merkle root, rows) from SEAL.json and the snapshot
    MANIFEST.json; their row counts must match the rows given. Merkle roots are not recomputed here.
    method "minhash" estimates Jaccard from policy.contamination.num_perm slots; "jaccard" is exact
    (SPEC.md allows it for small sets). status is "pass" only with no shared row hash, no near pair
    and seal_order.ok.
    """
    cfg = policy.contamination
    if cfg is None:
        raise ValueError("policy/gate.yaml has no contamination settings (minhash_threshold, num_perm, shingle)")
    for ref, rows, label in ((holdout, holdout_rows, "holdout"), (train, train_rows, "train")):
        if ref.rows is not None and ref.rows != len(rows):
            raise ValueError(f"{label} {ref.id} has rows: {ref.rows} but {len(rows)} rows were given")

    hold_hashes = _row_hashes(holdout_rows, "holdout")
    train_hashes = _row_hashes(train_rows, "train")
    shared = sorted(set(hold_hashes) & set(train_hashes))

    hold_sh = [(h, shingles(t, cfg.shingle)) for h, t in zip(hold_hashes, _texts(holdout_rows, texts, "holdout"))]
    train_sh = [(h, shingles(t, cfg.shingle)) for h, t in zip(train_hashes, _texts(train_rows, texts, "train"))]
    near = _near(hold_sh, train_sh, cfg, method)

    clean = not shared and not near.pairs and seal_order.ok
    return ContaminationReport(
        spec=SPEC,
        rebase=rebase,
        status="pass" if clean else "fail",
        holdout=holdout,
        train=train,
        exact=ExactOverlap(shared=len(shared), row_hashes=shared),
        near=near,
        seal_order=seal_order,
        synthetic=synthetic,
    )


def problems(report: ContaminationReport) -> list[str]:
    """Inconsistencies JSON Schema cannot express. An empty list means the report is coherent.

    The schema checks status against the parts; this checks the parts against each other.
    """
    out = []
    ex, near = report.exact, report.near
    if ex.shared != len(ex.row_hashes) or len(set(ex.row_hashes)) != len(ex.row_hashes):
        out.append(f"exact.shared is {ex.shared} but row_hashes lists {len(set(ex.row_hashes))} distinct hashes")
    if near.max_jaccard >= near.threshold and not near.pairs:
        out.append(f"near.max_jaccard {near.max_jaccard} >= threshold {near.threshold} but no pair is listed")
    for p in near.pairs:
        if p.jaccard < near.threshold:
            out.append(f"near pair {p.holdout[:12]}/{p.train[:12]} has jaccard {p.jaccard} below threshold {near.threshold}")
        if p.jaccard > near.max_jaccard:
            out.append(f"near pair {p.holdout[:12]}/{p.train[:12]} has jaccard {p.jaccard} above max_jaccard {near.max_jaccard}")
    clean = not ex.shared and not near.pairs and report.seal_order.ok
    if (report.status == "pass") != clean:
        out.append(f"status {report.status} does not match the exact, near and seal_order parts")
    return out
