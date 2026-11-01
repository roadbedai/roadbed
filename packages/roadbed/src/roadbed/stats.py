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
    Slices,
)
from roadbed.hashing import sha256_canonical
from roadbed.scoring import score_row

_CHUNK = 1000  # bootstrap resamples per vectorised block, bounds memory


def bootstrap_ci(diffs: np.ndarray, *, n_boot: int, ci: float, seed: int, stream: str) -> tuple[float, float]:
    """Percentile CI at level `ci` of the mean of paired differences (spec/METHOD.md).

    The RNG is numpy's default Generator (PCG64) seeded with [seed, crc32(stream)], so one slice's CI does
    not depend on which other slices exist or their order. Resample indices are drawn in blocks of
    _CHUNK resamples as rng.integers(0, n, size=(block, n)); the CI is np.quantile (linear) of the
    resample means at (1 - ci) / 2 and 1 - (1 - ci) / 2. This exact procedure is the reproducibility contract.
    """
    if diffs.ndim != 1 or diffs.shape[0] == 0:
        raise ValueError("bootstrap needs a non-empty 1-d array of paired differences")
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


# Floating-point noise must not move a slice across the margin: a delta of exactly -2.0 can come out of
# the mean as -2.0000000000000004. A slice is below the margin when delta < margin - MARGIN_EPS.
MARGIN_EPS = 1e-9


def below_margin(delta: float, n: int, policy: GatePolicy) -> bool:
    """SPEC.md, Scoring and gate, rule 5: delta < margin and not low-n."""
    return delta < policy.margin - MARGIN_EPS and n >= policy.min_n


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
    incumbent_predictions: Sequence[Mapping[str, Any]],
    candidate_predictions: Sequence[Mapping[str, Any]],
    holdout: Sequence[Mapping[str, Any]],
    slices: Slices | Sequence[str],
    policy: GatePolicy,
    *,
    scorers: Scorers,
    synthetic: bool,
    model: str,
    rebase: int,
    holdout_digest: HoldoutDigest | None = None,
    prediction_digests: PredictionDigests | None = None,
    notice: str | None = None,
) -> Scores:
    """Score both prediction sets against the holdout and compare them (spec/METHOD.md).

    Every holdout id must have exactly one incumbent and one candidate prediction.
    slices is evals/slices.yaml (or just its keys). Slices are the observed values of each key,
    keys in the given order and values sorted. With a Slices model, a holdout value that slices.yaml
    does not list is an error. A row without a key is in no slice of that key.
    Rows are processed in id order, so the input row order does not change any number.
    When holdout_digest/prediction_digests are not given, they are the canonical-JSON sha256 of the
    rows with path "inline".
    """
    hold = _index(holdout, "holdout")
    inc_by_id = _index(incumbent_predictions, "incumbent predictions")
    cand_by_id = _index(candidate_predictions, "candidate predictions")
    for label, preds in (("incumbent", inc_by_id), ("candidate", cand_by_id)):
        if set(preds) != set(hold):
            missing = sorted(set(hold) - set(preds))[:3]
            extra = sorted(set(preds) - set(hold))[:3]
            raise ValueError(f"{label} predictions do not match holdout ids (missing {missing}, extra {extra})")
    if not hold:
        raise ValueError("holdout is empty")
    allowed: dict[str, list[str]] | None = None
    if isinstance(slices, Slices):
        allowed = slices.keys
        slice_keys: list[str] = list(allowed)
    else:
        slice_keys = [str(k) for k in slices]

    ids = sorted(hold)  # row order of the input files must not change the CI
    inc = np.array([score_row(hold[i]["expected"], inc_by_id[i]["output"], scorers) for i in ids])
    cand = np.array([score_row(hold[i]["expected"], cand_by_id[i]["output"], scorers) for i in ids])

    overall = Overall(**_summary(inc, cand, policy, "overall"))

    results: list[SliceResult] = []
    for key in slice_keys:
        values = np.array([str(hold[i]["slices"].get(key, "")) for i in ids])
        observed = sorted(set(values.tolist()) - {""})
        if allowed is not None:
            unknown = [v for v in observed if v not in allowed[key]]
            if unknown:
                raise ValueError(f"holdout slice {key} has values {unknown[:3]} that slices.yaml does not list")
        for value in observed:
            mask = values == value
            s = _summary(inc[mask], cand[mask], policy, f"{key}={value}")
            results.append(
                SliceResult(
                    key=key,
                    value=value,
                    **s,
                    low_n=s["n"] < policy.min_n,
                    below_margin=below_margin(s["delta"], s["n"], policy),
                )
            )

    if holdout_digest is None:
        holdout_digest = HoldoutDigest(id="inline", rows=len(ids), sha256=sha256_canonical(list(holdout)))
    if prediction_digests is None:
        prediction_digests = PredictionDigests(
            incumbent=FileDigest(path="inline", sha256=sha256_canonical(list(incumbent_predictions))),
            candidate=FileDigest(path="inline", sha256=sha256_canonical(list(candidate_predictions))),
        )

    return Scores(
        spec=SPEC,
        synthetic=synthetic,
        model=model,
        rebase=rebase,
        seed=policy.seed,
        n_boot=policy.n_boot,
        ci=policy.ci,
        holdout=holdout_digest,
        predictions=prediction_digests,
        notice=notice,
        overall=overall,
        slices=results,
    )
