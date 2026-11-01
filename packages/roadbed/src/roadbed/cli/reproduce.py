"""`roadbed eval reproduce rebases/N`: recompute scores.json from the two prediction files, offline.

Uses the stored seed, n_boot and ci, the asset's holdout, scorers and slices, and roadbed.stats.compare. Every field
of the recomputed scores (overall, every slice, the holdout and prediction digests) must equal scores.json after
rounding floats to DECIMALS places. The first difference is a V-SCORES-MISMATCH failure that names the field.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from roadbed.asset import Check, sha256_file
from roadbed.cli.asset import PRED_FILES, Asset, load_rows
from roadbed.contracts import HoldoutDigest, PredictionDigests, Receipt, Scores
from roadbed.stats import compare

DECIMALS = 6


@dataclass
class Reproduced:
    stored: Scores
    recomputed: Scores
    executor: str | None
    check: Check


def first_diff(a: Any, b: Any, path: str = "") -> str | None:
    """Path of the first difference. Floats compare after rounding to DECIMALS places; bools compare as bools."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            p = f"{path}.{k}" if path else k
            if k not in a or k not in b:
                return f"{p} (only in {'scores.json' if k in a else 'the recomputation'})"
            if (d := first_diff(a[k], b[k], p)) is not None:
                return d
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{path} (length {len(a)} vs {len(b)})"
        for i, (x, y) in enumerate(zip(a, b)):
            if (d := first_diff(x, y, f"{path}[{i}]")) is not None:
                return d
        return None
    if isinstance(a, bool) or isinstance(b, bool):
        return None if a is b else f"{path} ({a} vs {b})"
    if isinstance(a, int | float) and isinstance(b, int | float):
        if math.isclose(round(a, DECIMALS), round(b, DECIMALS), abs_tol=10 ** -(DECIMALS + 1)):
            return None
        return f"{path} ({a} vs {b})"
    return None if a == b else f"{path} ({a!r} vs {b!r})"


def reproduce(asset: Asset, d: Path, n: int) -> Reproduced:
    raw = json.loads((d / "scores.json").read_text(encoding="utf-8"))
    stored = Scores.model_validate(raw)
    receipt = None
    if (d / "receipt.json").is_file():
        receipt = Receipt.model_validate(json.loads((d / "receipt.json").read_text(encoding="utf-8")))
    ex = receipt.predicate.executor.name if receipt is not None else None
    executor = str(getattr(ex, "value", ex)) if ex is not None else None
    policy = asset.policy.model_copy(update={"seed": stored.seed, "n_boot": stored.n_boot, "ci": stored.ci})
    hold, inc, cand = load_rows(asset, d)
    recomputed = compare(
        inc, cand, hold, asset.slices, policy,
        scorers=asset.scorers, synthetic=stored.synthetic, model=stored.model, rebase=n,
        holdout_digest=HoldoutDigest(id=stored.holdout.id, rows=len(hold), sha256=sha256_file(asset.holdout_file)),
        prediction_digests=PredictionDigests.model_validate({
            k: {"path": asset.rel(d / PRED_FILES[k]), "sha256": sha256_file(d / PRED_FILES[k])} for k in ("incumbent", "candidate")
        }),
        notice=stored.notice,
    )
    label = "scores recomputed: match"
    if stored.rebase != n:
        return Reproduced(stored, recomputed, executor,
                          Check("V-SCORES-MISMATCH", label, False, f"scores.json is for rebase {stored.rebase}, directory is {n}"))
    where = first_diff(raw, json.loads(recomputed.model_dump_json(exclude_none=True)))
    if where:
        return Reproduced(stored, recomputed, executor, Check("V-SCORES-MISMATCH", label, False, f"scores.json differs at {where}"))
    return Reproduced(stored, recomputed, executor,
                      Check("V-SCORES-MISMATCH", label, True, f"overall + {len(recomputed.slices)} slices, {DECIMALS} decimals"))
