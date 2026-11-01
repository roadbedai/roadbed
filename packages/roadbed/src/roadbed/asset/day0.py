"""The Day 0 checks of `roadbed verify`: rebase manifest hashes, scores.json and gate.json schemas, scores recomputed,
seal time before the rebase. Moved here from roadbed.cli.verify (W02); their output for a Day 0 rebase is unchanged."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema

from roadbed.asset.files import safe_rel
from roadbed.cli.asset import MANIFEST, PRED_FILES, Asset, bound_files, load_rows, read_meta, seal_time
from roadbed.contracts import HoldoutDigest, PredictionDigests, json_schema
from roadbed.hashing import sha256_file
from roadbed.stats import compare

DECIMALS = 6


@dataclass
class Check:
    code: str  # V-... code, used when the check fails
    label: str  # text shown when it passes
    ok: bool | None  # None = skipped
    detail: str = ""


class VerifyFailed(Exception):
    def __init__(self, check: Check) -> None:
        super().__init__(f"{check.code}: {check.detail}")
        self.check = check


def _hashes(asset: Asset, d: Path, required: list[str] | None = None) -> Check:
    """Manifest hashes. `required` (full rebases, roadbed.asset.verify) lists every path the manifest must name;
    each must exist. Without it, the Day 0 rule applies: a rebase with receipt.json must list the bound files."""
    m = d / MANIFEST
    if not m.is_file():
        return Check("V-MANIFEST-MISSING", "hashes OK", False, f"{asset.rel(m)} not found")
    count = 0
    listed: set[str] = set()
    for lineno, line in enumerate(m.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        digest, sep, rel = line.partition("  ")
        if not sep or len(digest) != 64:
            return Check("V-MANIFEST-MALFORMED", "hashes OK", False, f"{asset.rel(m)}:{lineno}")
        if not safe_rel(rel):
            return Check("V-MANIFEST-MALFORMED", "hashes OK", False, f"{asset.rel(m)}:{lineno}: path outside the asset")
        f = asset.root / rel
        if f.is_symlink() or not f.is_file():
            return Check("V-HASH-MISSING", "hashes OK", False, f"{rel} listed in {MANIFEST} but not found")
        if sha256_file(f) != digest:
            return Check("V-HASH-MISMATCH", "hashes OK", False, f"{rel} does not match {MANIFEST}")
        count += 1
        listed.add(rel)
    if required is not None:
        for rel in required:
            if not safe_rel(rel):
                return Check("V-MANIFEST-MALFORMED", "hashes OK", False, f"bound path outside the asset: {rel!r}")
            if not (asset.root / rel).is_file():
                return Check("V-HASH-MISSING", "hashes OK", False, f"{rel} not found (a rebase with a receipt, evidence or lock is not Day 0)")
        missing = [rel for rel in required if rel not in listed]
        if missing:
            return Check("V-MANIFEST-INCOMPLETE", "hashes OK", False, f"{MANIFEST} does not list {', '.join(missing)}")
        return Check("V-HASH-MISMATCH", "hashes OK", True, f"{count} files")
    # A rebase with a receipt binds the seal, the snapshot manifest and the recipe (SPEC.md, Manifests).
    # A Day 0 rebase has no receipt; its manifest needs only what it lists.
    if (d / "receipt.json").is_file():
        missing = [asset.rel(f) for f in bound_files(asset) if asset.rel(f) not in listed]
        if missing:
            return Check("V-MANIFEST-INCOMPLETE", "hashes OK", False, f"{MANIFEST} does not list {', '.join(missing)}")
    return Check("V-HASH-MISMATCH", "hashes OK", True, f"{count} files")


def _schemas(d: Path) -> Check:
    for name, schema in (("scores.json", "scores"), ("gate.json", "gate")):
        f = d / name
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator(json_schema(schema, reader=True)).validate(data)  # readers ignore unknown fields
        except FileNotFoundError:
            return Check("V-SCHEMA-INVALID", "schemas OK", False, f"{name} not found")
        except (json.JSONDecodeError, jsonschema.ValidationError) as e:
            msg = e.message if isinstance(e, jsonschema.ValidationError) else str(e)
            return Check("V-SCHEMA-INVALID", "schemas OK", False, f"{name}: {msg}")
    return Check("V-SCHEMA-INVALID", "schemas OK", True, "scores.json, gate.json")


def _diff(a: Any, b: Any, path: str = "") -> str | None:
    """First path where a and b differ. Floats compare after rounding to DECIMALS."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}.{k}".lstrip(".")
            d = _diff(a[k], b[k], f"{path}.{k}")
            if d:
                return d.lstrip(".")
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{path} (length {len(a)} vs {len(b)})"
        for i, (x, y) in enumerate(zip(a, b)):
            d = _diff(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    if isinstance(a, bool) or isinstance(b, bool):
        return None if a is b else path
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if math.isclose(round(a, DECIMALS), round(b, DECIMALS), abs_tol=10 ** -(DECIMALS + 1)):
            return None
        return f"{path} ({a} vs {b})"
    return None if a == b else path


def _scores(asset: Asset, d: Path, n: int) -> Check:
    stored = json.loads((d / "scores.json").read_text(encoding="utf-8"))
    policy = asset.policy.model_copy(update={"seed": stored["seed"], "n_boot": stored["n_boot"], "ci": stored["ci"]})
    hold, inc, cand = load_rows(asset, d)
    recomputed = compare(
        inc, cand, hold, asset.slices, policy,
        scorers=asset.scorers, synthetic=stored["synthetic"], model=stored["model"], rebase=stored["rebase"],
        holdout_digest=HoldoutDigest(id=stored["holdout"]["id"], rows=len(hold), sha256=sha256_file(asset.holdout_file)),
        prediction_digests=PredictionDigests.model_validate({
            k: {"path": asset.rel(d / PRED_FILES[k]), "sha256": sha256_file(d / PRED_FILES[k])}
            for k in ("incumbent", "candidate")
        }),
        notice=stored.get("notice"),
    )
    if stored["rebase"] != n:
        return Check("V-SCORES-MISMATCH", "scores recomputed: match", False, f"rebase {stored['rebase']} in a directory named {n}")
    where = _diff(stored, json.loads(recomputed.model_dump_json(exclude_none=True)))
    if where:
        return Check("V-SCORES-MISMATCH", "scores recomputed: match", False, f"scores.json differs at {where}")
    return Check("V-SCORES-MISMATCH", "scores recomputed: match", True, f"overall + {len(recomputed.slices)} slices, {DECIMALS} decimals")


def _when(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _seal(asset: Asset, d: Path) -> Check:
    meta = read_meta(d)
    label = "seal precedes rebase: OK"
    if not meta or "created_at" not in meta:
        return Check("V-SEAL-AFTER-REBASE", "seal precedes rebase: skipped (no rebase.yaml)", None)
    sealed, created = _when(seal_time(asset)), _when(meta["created_at"])
    if not sealed < created:
        return Check("V-SEAL-AFTER-REBASE", label, False, f"holdout sealed {sealed.isoformat()} is not before rebase created {created.isoformat()}")
    return Check("V-SEAL-AFTER-REBASE", label, True)
