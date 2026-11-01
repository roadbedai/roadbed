"""Paired bootstrap comparison of incumbent and candidate, overall and per slice."""

from __future__ import annotations

import zlib
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from roadbed.contracts import (
    SPEC,
    FileDigest,
    GatePolicy,
    HoldoutDigest,
    Overall,
    PredictionDigests,
    Scorers,
    Scores,
    SliceResult,
)
from roadbed.hashing import sha256_canonical
from roadbed.scoring import score_row

_CHUNK = 1000  # bootstrap resamples per vectorised block, bounds memory


def bootstrap_ci(diffs: np.ndarray, *, n_boot: int, ci: float, seed: int, stream: str) -> tuple[float, float]:
    """Percentile CI at level `ci` of the mean of paired differences.

    The RNG is seeded from (seed, crc32(stream)), so one slice's CI does not
    depend on which other slices exist or their order.
    """
    n = diffs.shape[0]
    rng = np.random.default_rng([seed, zlib.crc32(stream.encode("utf-8"))])
    means = np.empty(n_boot, dtype=np.float64)
    for start in range(0, n_boot, _CHUNK):
        stop = min(start + _CHUNK, n_boot)
        idx = rng.integers(0, n, size=(stop - start, n))
        means[start:stop] = diffs[idx].mean(axis=1)
    alpha = (1.0 - ci) / 2.0
    lo, hi = np.quantile(means, [alpha, 1.0 - alpha])
    return float(lo), float(hi)


def _summary(inc: np.ndarray, cand: np.ndarray, policy: GatePolicy, stream: str) -> dict[str, Any]:
    diffs = cand - inc
    lo, hi = bootstrap_ci(diffs, n_boot=policy.n_boot, ci=policy.ci, seed=policy.seed, stream=stream)
    return {
        "n": int(diffs.shape[0]),
        "incumbent": float(inc.mean()),
        "candidate": float(cand.mean()),
        "delta": float(diffs.mean()),
        "ci_low": lo,
        "ci_high": hi,
    }


def _index(rows: Sequence[Mapping[str, Any]], label: str) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for r in rows:
        if r["id"] in out:
            raise ValueError(f"duplicate id {r['id']!r} in {label}")
        out[r["id"]] = r
    return out


def compare(
    holdout_rows: Sequence[Mapping[str, Any]],
    incumbent_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    slice_keys: Sequence[str],
    policy: GatePolicy,
    *,
    scorers: Scorers,
    synthetic: bool,
    model: str,
    rebase: int,
    holdout: HoldoutDigest | None = None,
    predictions: PredictionDigests | None = None,
    notice: str | None = None,
) -> Scores:
    """Score both prediction sets against the holdout and compare them.

    Every holdout id must have exactly one incumbent and one candidate prediction.
    Slices are the observed values of each key in slice_keys, keys in the given
    order and values sorted. Rows are processed in id order. When holdout/predictions digests are not given,
    they are the canonical-JSON sha256 of the rows with path "inline".
    """
    hold = _index(holdout_rows, "holdout")
    inc_by_id = _index(incumbent_rows, "incumbent predictions")
    cand_by_id = _index(candidate_rows, "candidate predictions")
    for label, preds in (("incumbent", inc_by_id), ("candidate", cand_by_id)):
        if set(preds) != set(hold):
            missing = sorted(set(hold) - set(preds))[:3]
            extra = sorted(set(preds) - set(hold))[:3]
            raise ValueError(f"{label} predictions do not match holdout ids (missing {missing}, extra {extra})")
    if not hold:
        raise ValueError("holdout is empty")

    ids = sorted(hold)  # row order of the input files must not change the CI
    inc = np.array([score_row(hold[i]["expected"], inc_by_id[i]["output"], scorers) for i in ids])
    cand = np.array([score_row(hold[i]["expected"], cand_by_id[i]["output"], scorers) for i in ids])

    overall = Overall(**_summary(inc, cand, policy, "overall"))

    slices: list[SliceResult] = []
    for key in slice_keys:
        values = np.array([str(hold[i]["slices"].get(key, "")) for i in ids])
        for value in sorted(set(values.tolist()) - {""}):
            mask = values == value
            s = _summary(inc[mask], cand[mask], policy, f"{key}={value}")
            low_n = s["n"] < policy.min_n
            slices.append(
                SliceResult(
                    key=key,
                    value=value,
                    **s,
                    low_n=low_n,
                    below_margin=(s["delta"] < policy.margin) and not low_n,
                )
            )

    if holdout is None:
        holdout = HoldoutDigest(id="inline", rows=len(ids), sha256=sha256_canonical(list(holdout_rows)))
    if predictions is None:
        predictions = PredictionDigests(
            incumbent=FileDigest(path="inline", sha256=sha256_canonical(list(incumbent_rows))),
            candidate=FileDigest(path="inline", sha256=sha256_canonical(list(candidate_rows))),
        )

    return Scores(
        spec=SPEC,
        synthetic=synthetic,
        model=model,
        rebase=rebase,
        seed=policy.seed,
        n_boot=policy.n_boot,
        ci=policy.ci,
        holdout=holdout,
        predictions=predictions,
        notice=notice,
        overall=overall,
        slices=slices,
    )
