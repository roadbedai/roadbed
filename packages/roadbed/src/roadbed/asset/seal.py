"""Seal a holdout: write holdout.jsonl, the MinHash object and SEAL.json once (SPEC.md, Seal precedes training).

Only a validator seals (SoD-05); the control plane enforces the role, this library writes the files. A local seal
(provenance local, `roadbed evals seal`) names a person, has no audit seq and is always a full seal. The holdout rows
carry no raw input, only its content address; the raw inputs are read here to build the MinHash signatures and are
not written to the asset.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from roadbed.asset.files import SYNTHETIC_MD, AssetValidationError, validate
from roadbed.asset.hashing import jcs_sha256, merkle_root, sha256_bytes, sha256_file
from roadbed.asset.objects import ObjectStore
from roadbed.asset.overlap import DEFAULT_NUM_PERM, DEFAULT_SHINGLE, minhash, minhash_object, shingles
from roadbed.io import write_jsonl

SEAL = "SEAL.json"
HOLDOUT = "holdout.jsonl"
SYNTHETIC_TEXT = "# SYNTHETIC\n\nSynthetic holdout rows. Illustrative data, not from any real tenant or model.\n"


class SealError(Exception):
    pass


def holdout_row_hash(row: Mapping[str, Any]) -> str:
    """The row hash a holdout row carries; a Day 0 row without one uses sha256(JCS(row)) (SPEC.md, Hashing rule 4)."""
    h = row.get("row_hash")
    return h if isinstance(h, str) else jcs_sha256(dict(row))


def seal_holdout(
    root: str | Path,
    holdout_id: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    sealed_by: str,
    sealed_at: str,
    audit_seq: int | None,
    seed: int,
    synthetic: bool,
    stratification: Mapping[str, Any] | None = None,
    snapshot: str | None = None,
    inputs: Mapping[str, str] | None = None,
    store: ObjectStore | None = None,
    num_perm: int = DEFAULT_NUM_PERM,
    shingle: int = DEFAULT_SHINGLE,
    provenance: Literal["control_plane", "local"] = "control_plane",
) -> dict[str, Any]:
    """Write data/holdout/<id>/{holdout.jsonl, SEAL.json} under root and return the SEAL document.

    A control-plane seal needs audit_seq. A local seal (provenance="local") takes audit_seq=None and must be full;
    SEAL.json then carries provenance: local and no audit_seq.

    A full seal needs stratification, snapshot, `inputs` (input_ref -> raw text, for every row) and a `store` for
    the MinHash object. Without them it is a Day 0 seal, which supports no near-overlap check. Refuses to write over
    an existing seal. Every row is validated against the closed holdout-row schema, so no raw input field gets in.
    """
    full = stratification is not None or snapshot is not None or inputs is not None
    if full and (stratification is None or snapshot is None or inputs is None or store is None):
        raise SealError("a full seal needs stratification, snapshot, inputs and a store")
    d = Path(root) / "data" / "holdout" / holdout_id
    if (d / SEAL).exists() or (d / HOLDOUT).exists():
        raise SealError(f"{holdout_id} is already sealed: a seal is written once")
    out = [dict(r) for r in rows]
    for i, r in enumerate(out, 1):
        try:
            validate(r, "holdout-row", strict=True)
        except AssetValidationError as e:
            raise SealError(f"holdout row {i}: {e}") from None
        if full and "row_hash" not in r:
            raise SealError(f"holdout row {i}: a full seal needs the row_hash of the source verdict row")
    hashes = [holdout_row_hash(r) for r in out]
    for what, vals in (("id", [r["id"] for r in out]), ("row_hash", hashes), ("input_ref", [r["input_ref"] for r in out])):
        if len(set(vals)) != len(vals):
            raise SealError(f"holdout rows repeat a {what}")

    seal: dict[str, Any] = {
        "id": holdout_id, "sealed_by": sealed_by, "sealed_at": sealed_at, "audit_seq": audit_seq, "seed": seed,
        "rows": len(out), "sha256": "", "synthetic": synthetic,
    }
    if audit_seq is None:
        del seal["audit_seq"]
    if provenance == "local":
        seal["provenance"] = "local"
    blob = b""
    if full:
        assert inputs is not None and stratification is not None
        sigs = {}
        for r, h in zip(out, hashes):
            text = inputs.get(r["input_ref"])
            if text is None:
                raise SealError(f"no raw input for {r['id']}")
            if "sha256:" + sha256_bytes(text.encode("utf-8")) != r["input_ref"]:
                raise SealError(f"raw input of {r['id']} does not match its input_ref")
            sigs[h] = minhash(shingles(text, shingle), num_perm)
        blob = minhash_object(sigs, num_perm, shingle)
        seal.update(stratification=dict(stratification), merkle_root=merkle_root(hashes),
                    minhash_oid=sha256_bytes(blob), snapshot=snapshot)
    try:
        validate({**seal, "sha256": "0" * 64}, "seal", strict=True)
    except AssetValidationError as e:
        raise SealError(f"SEAL.json: {e}") from None

    if blob:
        assert store is not None
        store.put(blob)
    write_jsonl(d / HOLDOUT, out)
    seal["sha256"] = sha256_file(d / HOLDOUT)
    with open(d / SEAL, "x", encoding="utf-8") as f:  # "x": never overwrite a seal
        f.write(json.dumps(seal, indent=2, ensure_ascii=False) + "\n")
    if synthetic and not (d / SYNTHETIC_MD).exists():
        (d / SYNTHETIC_MD).write_text(SYNTHETIC_TEXT, encoding="utf-8")
    return seal
