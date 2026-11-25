"""The asset library against spec/conformance: every valid case validates, every invalid case is refused, and an invalid case
planted in a real asset (re-hashed and re-signed, so only the schema can catch it) fails verify with V-SCHEMA-INVALID."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from roadbed.asset import AssetValidationError, validate, verify
from test_asset_support import CONFORMANCE, copy, reseal

VALID = sorted(CONFORMANCE.glob("valid/*/*.json"))
INVALID = sorted(CONFORMANCE.glob("invalid/*/*.json"))

# Where each asset-file schema lives in the pass fixture. JSONL targets replace line 1.
TARGET = {
    "roadbed": "roadbed.yaml",
    "holdout-row": "data/holdout/H-2026-11-01/holdout.jsonl",
    "prediction-row": "rebases/13/predictions.candidate.jsonl",
    "scorers": "evals/scorers.yaml",
    "slices": "evals/slices.yaml",
    "gate-policy": "policy/gate.yaml",
    "license-policy": "policy/license.yaml",
    "scores": "rebases/13/scores.json",
    "gate": "rebases/13/gate.json",
    "lock": "roadbed.lock",
    "object-pointer": "data/weights.bin.ptr",
    "snapshot-manifest": "data/snapshots/2026-11-01/MANIFEST.json",
    "snapshot-row": "data/snapshots/2026-11-01/part-0000.jsonl",
    "seal": "data/holdout/H-2026-11-01/SEAL.json",
    "tombstone-row": "data/tombstones.jsonl",
    "golden-row": "evals/golden.jsonl",
    "correction-test-row": "evals/correction_tests.jsonl",
    "recipe": "recipes/qwen3/lora-r16-sft@v4.yaml",
    "model-version": "versions/v12/model.json",
    "receipt": "rebases/13/receipt.json",
    "signature": "rebases/13/receipt.sig",
    "rebase": "rebases/13/rebase.yaml",
    "contamination": "rebases/13/contamination.json",
    "cost": "rebases/13/cost.json",
    "license-decision": "rebases/13/license.json",
    "approvals": "rebases/13/evidence/approvals.json",
    "finding": "findings/F-0001.yaml",
    "lineage-edge": "lineage/edges.jsonl",
    "audit-anchor": "audit/anchors/2026-11-01.json",
    "evidence-manifest": "rebases/13/evidence/MANIFEST.sha256",
}
ASSET_CASES = [p for p in INVALID if p.parent.name in TARGET]


def _id(p: Path) -> str:
    return f"{p.parent.name}/{p.stem}"


@pytest.mark.parametrize("case", VALID, ids=_id)
def test_valid_cases_validate(case):
    validate(json.loads(case.read_text()), case.parent.name, strict=True)


@pytest.mark.parametrize("case", INVALID, ids=_id)
def test_invalid_cases_are_refused(case):
    with pytest.raises(AssetValidationError):
        validate(json.loads(case.read_text()), case.parent.name, strict=True)


def _plant(root: Path, schema: str, doc: dict) -> None:
    rel = TARGET[schema]
    f = root / rel
    if schema == "evidence-manifest":
        f.write_text("".join(f"{e['sha256']}  {e['path']}\n" for e in doc["entries"]))
    elif rel.endswith(".jsonl"):
        lines = f.read_text().splitlines()
        f.write_text("\n".join([json.dumps(doc), *lines[1:]]) + "\n")
    else:
        f.write_text(json.dumps(doc, indent=2) + "\n")  # JSON is YAML too


@pytest.mark.parametrize("case", ASSET_CASES, ids=_id)
def test_invalid_case_in_an_asset_fails_verify(case, tmp_path):
    schema = case.parent.name
    root = copy("pass", tmp_path / "a")
    _plant(root, schema, json.loads(case.read_text()))
    if schema not in ("roadbed", "gate-policy", "scorers", "slices"):  # these stop the asset from opening at all
        reseal(root, 13, sign=schema not in ("signature", "receipt"), relock=schema != "lock")
    r = verify(root / "rebases/13", strict=True)
    assert r.failed is not None, case
    assert r.failed.code == "V-SCHEMA-INVALID", (r.failed.code, r.failed.detail)


def test_every_asset_schema_has_a_planted_case():
    planted = {p.parent.name for p in ASSET_CASES}
    missing = sorted(set(TARGET) - planted)
    assert missing == [], f"no invalid conformance case for {missing}"
