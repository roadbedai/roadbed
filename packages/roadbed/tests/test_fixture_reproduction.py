"""Recompute spec/examples/*/rebases/N/{scores,gate,contamination}.json from their inputs with the stats interface.

Inputs only: the two prediction files, the holdout, evals/{scorers,slices}.yaml, policy/gate.yaml, license.json,
the snapshot rows and the raw input texts. The stored outputs are read only to compare bytes at the end.
The fixtures are SYNTHETIC and their receipts say executor: mock; these tests check the engine, not a model.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import jsonschema
import pytest
import rfc8785

from conftest import SPEC_DIR
from roadbed import contamination as contamination_mod
from roadbed.cli.asset import NOTICE
from roadbed.contracts import (
    ContaminationReport,
    DatasetRef,
    FileDigest,
    GatePolicy,
    HoldoutDigest,
    LicenseDecisionRecord,
    LicensePolicy,
    PredictionDigests,
    SealOrder,
    Slices,
    json_schema,
)
from roadbed.gate import decide_license, evaluate
from roadbed.hashing import sha256_file
from roadbed.io import read_jsonl, read_yaml
from roadbed.scoring import load_scorers
from roadbed.stats import compare

EXAMPLES = SPEC_DIR / "examples"
CASES = [("pass", 13), ("blocked", 14), ("claims-triage", 1)]


def _builder():
    spec = importlib.util.spec_from_file_location("build_examples", SPEC_DIR / "tools" / "build_examples.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dump(model) -> str:
    """The writer form `roadbed rebase` uses for scores.json and gate.json."""
    return model.model_dump_json(indent=2, exclude_none=True) + "\n"


def _finding_offset(root: Path, n: int) -> int:
    top = 0
    for f in sorted((root / "findings").glob("F-*.yaml")) if (root / "findings").is_dir() else []:
        if (read_yaml(f) or {}).get("rebase") != n:
            top = max(top, int(f.stem.removeprefix("F-")))
    return top


def _recompute(asset: str, n: int):
    root = EXAMPLES / asset
    d = root / f"rebases/{n}"
    cfg = read_yaml(root / "roadbed.yaml")
    policy = GatePolicy.model_validate(read_yaml(root / cfg["gate"]))
    holdout_file = root / cfg["data"]["holdout"]["path"] / "holdout.jsonl"
    hold = read_jsonl(holdout_file)
    preds = {k: d / f"predictions.{k}.jsonl" for k in ("incumbent", "candidate")}
    scores = compare(
        read_jsonl(preds["incumbent"]), read_jsonl(preds["candidate"]), hold,
        Slices.model_validate(read_yaml(root / cfg["evals"]["slices"])), policy,
        scorers=load_scorers(root / cfg["evals"]["scorers"]), synthetic=cfg["synthetic"], model=cfg["model"], rebase=n,
        holdout_digest=HoldoutDigest(id=cfg["data"]["holdout"]["id"], rows=len(hold), sha256=sha256_file(holdout_file)),
        prediction_digests=PredictionDigests(**{
            k: FileDigest(path=p.relative_to(root).as_posix(), sha256=sha256_file(p)) for k, p in preds.items()
        }),
        notice=NOTICE,
    )
    if (d / "contamination.json").exists():
        cont = ContaminationReport.model_validate_json((d / "contamination.json").read_text())
        lic = LicenseDecisionRecord.model_validate_json((d / "license.json").read_text())
    else:  # Day 0 example: no train manifest, license from policy/license.yaml for the apache-2.0 base
        cont = None
        lic = decide_license("apache-2.0", LicensePolicy.model_validate(read_yaml(root / "policy/license.yaml")))
    gate = evaluate(scores, cont, lic, policy, finding_offset=_finding_offset(root, n))
    return d, scores, gate


@pytest.mark.parametrize("asset,n", CASES)
def test_scores_and_gate_reproduce_byte_for_byte(asset, n):
    d, scores, gate = _recompute(asset, n)
    for name, model in (("scores", scores), ("gate", gate)):
        out = _dump(model)
        jsonschema.Draft202012Validator(json_schema(name)).validate(json.loads(out))
        assert out == (d / f"{name}.json").read_text(encoding="utf-8"), f"{asset}: {name}.json differs"


def test_fixture_outcomes_are_the_plan_cases():
    _, s, g = _recompute("pass", 13)
    assert g.status == "pass" and g.findings == []
    _, s, g = _recompute("blocked", 14)
    bi = next(x for x in s.slices if x.value == "bodily_injury")
    # bodily_injury is rows 0-29. The candidate gets the route wrong on rows 0-14, the incumbent on rows
    # 0, 6, 12, 18, 24 (row score 40 when wrong). Only the candidate is wrong on 12 rows (-60 each), only the
    # incumbent on 2 (+60 each): delta = (-12 * 60 + 2 * 60) / 30 = -20.0
    assert (bi.n, bi.delta, bi.below_margin) == (30, -20.0, True)
    assert g.status == "blocked" and [f.slice for f in g.findings] == ["line=bodily_injury"]


@pytest.mark.parametrize("asset,n", [("pass", 13), ("blocked", 14)])
def test_contamination_reproduces_from_rows_and_texts(asset, n):
    """Exact and near overlap recomputed from the snapshot, the holdout and the raw texts (the SYNTHETIC
    narratives the builder hashed into input_ref). The seal order needs git: it belongs to the asset library, taken as given."""
    b = _builder()
    root = EXAMPLES / asset
    d = root / f"rebases/{n}"
    stored = json.loads((d / "contamination.json").read_text())
    policy = GatePolicy.model_validate(read_yaml(root / "policy/gate.yaml"))
    hold = read_jsonl(root / "data/holdout/H-2026-11-01/holdout.jsonl")
    train = read_jsonl(root / "data/snapshots/2026-11-01/part-0000.jsonl")
    texts = {"sha256:" + b.sha256_hex(t.encode()): t
             for t in [b.narrative("h", i) for i in range(len(hold))] + [b.narrative("t", j) for j in range(len(train))]}
    kw = dict(texts=texts, rebase=n, synthetic=True, seal_order=SealOrder.model_validate(stored["seal_order"]),
              holdout=DatasetRef.model_validate(stored["holdout"]), train=DatasetRef.model_validate(stored["train"]))
    exact = contamination_mod.check(train, hold, policy, method="jaccard", **kw)
    assert json.loads(exact.model_dump_json(exclude_none=True)) == stored
    near = contamination_mod.check(train, hold, policy, method="minhash", **kw)
    assert near.status == "pass" and near.near.pairs == [] and near.near.max_jaccard < 0.8


def test_minhash_matches_the_sealed_signature_object():
    """Known answer for the signature format: the holdout signatures hash to SEAL.json minhash_oid."""
    b = _builder()
    root = EXAMPLES / "blocked"
    seal = json.loads((root / "data/holdout/H-2026-11-01/SEAL.json").read_text())
    pol = GatePolicy.model_validate(read_yaml(root / "policy/gate.yaml")).contamination
    hold = read_jsonl(root / "data/holdout/H-2026-11-01/holdout.jsonl")
    sigs = {r["row_hash"]: contamination_mod.signature(contamination_mod.shingles(b.narrative("h", i), pol.shingle), pol.num_perm)
            for i, r in enumerate(hold)}
    blob = rfc8785.dumps({"num_perm": pol.num_perm, "shingle": pol.shingle, "signatures": sigs})
    assert b.sha256_hex(blob) == seal["minhash_oid"]
