"""Verify of an engine-scored rebase: runner receipts under rebases/N/receipts/, predictions, and scores.json and
gate.json from `roadbed rebase --from-predictions`, with no control-plane receipt.json, contamination.json,
license.json, bom.cdx.json or evidence/.

The fixture is spec/examples/pass rebase 13 rebuilt in that shape: the lock is written before the run (it does not
list the rebase), the engine scores the fixture predictions, then the runner receipts are added and signed with a
fixture runner key.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from roadbed.asset import build_lock, sign_file, verify, write_lock, write_signature
from roadbed.asset.signing import public_pem
from roadbed.cli.asset import open_asset
from roadbed.cli.main import app
from roadbed.cli.rebase import BaseRef
from roadbed.cli.rebase import run as rebase_run
from roadbed.hashing import sha256_file
from test_asset_support import EXAMPLES, _rehash, builder, copy, edit_json

runner = CliRunner()
RUNNER_KEY = "policy/keys/runner.pub"
N = 13
SKIPPED = [
    "bom.cdx.json: skipped",
    "contamination.json matches: skipped",
    "evidence manifest OK: skipped",
    "license.json: skipped",
    "lock lists every file: skipped",
    "receipt chain OK: skipped",
    "seal precedes training: git ancestry: skipped",
]


def _sign(root: Path, rel: str) -> None:
    key = builder().test_key("runner")
    write_signature(root / (rel.removesuffix(".json") + ".sig"), sign_file(key, root, rel, RUNNER_KEY, "jcs"))


def _receipts(root: Path) -> None:
    """Write and sign the runner receipts: train-candidate, eval-incumbent, eval-candidate. Each eval receipt names
    its prediction file by oid, as the runner does."""
    src = json.loads((EXAMPLES / f"pass/rebases/{N}/receipt.json").read_text())
    d = root / f"rebases/{N}"
    (d / "receipts").mkdir(exist_ok=True)
    jobs = [
        ("train-candidate", "train", "2026-11-01T10:20:00Z", 1377, builder().oid(f"claims-triage rebase {N} adapter")),
        ("eval-incumbent", "eval", "2026-11-01T10:21:00Z", 1378, sha256_file(d / "predictions.incumbent.jsonl")),
        ("eval-candidate", "eval", "2026-11-01T10:22:00Z", 1379, sha256_file(d / "predictions.candidate.jsonl")),
    ]
    for name, kind, start, seq, oid in jobs:
        p = {k: v for k, v in src["predicate"].items() if k not in ("prev_receipt", "config")}
        p.update(job={"id": f"job-{name}", "kind": kind}, started_at=start, ended_at=start.replace(":00Z", ":30Z"),
                 audit_seq=seq)
        doc = {"_type": src["_type"], "subject": [{"name": f"oid:{oid}", "digest": {"sha256": oid}}],
               "predicateType": src["predicateType"], "predicate": p}
        rel = f"rebases/{N}/receipts/{name}.json"
        (root / rel).write_text(json.dumps(doc, indent=2) + "\n")
        _sign(root, rel)


def engine_scored(dest: Path) -> Path:
    root = copy("pass", dest)
    d = root / f"rebases/{N}"
    keep = {f: (d / f).read_bytes() for f in ("predictions.incumbent.jsonl", "predictions.candidate.jsonl", "rebase.yaml")}
    shutil.rmtree(d)
    write_lock(root, build_lock(root, previous=json.loads((root / "roadbed.lock").read_text()), synthetic=True))
    d.mkdir()
    for f, b in keep.items():
        (d / f).write_bytes(b)
    (root / RUNNER_KEY).write_bytes(public_pem(builder().test_key("runner")))
    asset = open_asset(root)
    meta = yaml.safe_load((d / "rebase.yaml").read_text())
    rebase_run(asset, d, N, BaseRef(**meta["base"]), emit=lambda _: None)
    _receipts(root)
    return root


def rebind(root: Path) -> None:
    """An attacker with the runner key: re-hash the rebase manifest, re-name the predictions in the eval receipts
    and re-sign every runner receipt, so only the check under test can catch the change."""
    d = root / f"rebases/{N}"
    m = d / "MANIFEST.sha256"
    m.write_text(_rehash(root, m.read_text()))
    for who in ("incumbent", "candidate"):
        oid = sha256_file(d / f"predictions.{who}.jsonl")
        edit_json(d / f"receipts/eval-{who}.json",
                  lambda doc, oid=oid: doc.update(subject=[{"name": f"oid:{oid}", "digest": {"sha256": oid}}]))
    for f in sorted((d / "receipts").glob("*.json")):
        _sign(root, f.relative_to(root).as_posix())


@pytest.fixture
def asset(tmp_path) -> Path:
    return engine_scored(tmp_path / "a")


def _failed(root: Path):
    r = verify(root / f"rebases/{N}")
    assert r.failed is not None, [(c.code, c.ok, c.detail) for c in r.checks]
    return r.failed


def test_passes_with_named_skips(asset):
    r = verify(asset / f"rebases/{N}")
    assert r.ok, r.failed
    assert sorted(c.label for c in r.checks if c.ok is None) == SKIPPED
    passed = {c.code for c in r.checks if c.ok}
    assert {"V-HASH-MISMATCH", "V-SIG-INVALID", "V-RECEIPT-MISMATCH", "V-SCORES-MISMATCH", "V-SEAL-ORDER",
            "V-SEAL-INCOMPLETE"} <= passed


def test_strict_fails_on_a_skip(asset):
    r = verify(asset / f"rebases/{N}", strict=True)
    assert r.failed is not None and "--strict" in r.failed.detail


def test_flipped_prediction_byte(asset):
    f = asset / f"rebases/{N}/predictions.candidate.jsonl"
    b = bytearray(f.read_bytes())
    b[len(b) // 2] ^= 0x01
    f.write_bytes(bytes(b))
    fail = _failed(asset)
    assert fail.code == "V-HASH-MISMATCH" and "predictions.candidate.jsonl" in fail.detail


def test_predictions_not_named_by_the_eval_receipt(asset):
    f = asset / f"rebases/{N}/predictions.candidate.jsonl"
    f.write_text(f.read_text() + "\n")  # same rows, other bytes
    m = asset / f"rebases/{N}/MANIFEST.sha256"
    m.write_text(_rehash(asset, m.read_text()))
    fail = _failed(asset)
    assert fail.code == "V-RECEIPT-MISMATCH" and "eval-candidate" in fail.detail


def test_edited_scores(asset):
    edit_json(asset / f"rebases/{N}/scores.json", lambda d: d["overall"].update(candidate=d["overall"]["candidate"] + 0.5))
    rebind(asset)
    assert _failed(asset).code == "V-SCORES-MISMATCH"


def test_edited_gate(asset):
    edit_json(asset / f"rebases/{N}/gate.json", lambda d: d["checks"][0].update(detail="0 of 0 slices"))
    rebind(asset)
    fail = _failed(asset)
    assert fail.code == "V-SCORES-MISMATCH" and "gate.json" in fail.detail


@pytest.mark.parametrize("sig", [f"rebases/{N}/receipts/eval-candidate.sig", "versions/v12/receipt.sig"])
def test_missing_signature(asset, sig):
    (asset / sig).unlink()
    write_lock(asset, build_lock(asset, previous=json.loads((asset / "roadbed.lock").read_text()), synthetic=True))
    assert _failed(asset).code == "V-SIG-MISSING"


def test_bad_signature(asset):
    edit_json(asset / f"rebases/{N}/receipts/train-candidate.json", lambda d: d["predicate"].update(seed=1))
    assert _failed(asset).code == "V-SIG-INVALID"


def test_training_before_the_seal(asset):
    edit_json(asset / f"rebases/{N}/receipts/train-candidate.json",
              lambda d: d["predicate"].update(started_at="2026-11-01T09:00:00Z"))
    rebind(asset)
    assert _failed(asset).code == "V-SEAL-ORDER"


def test_predictions_cover_the_sealed_rows(asset):
    for who in ("incumbent", "candidate"):
        f = asset / f"rebases/{N}/predictions.{who}.jsonl"
        f.write_text("".join(f.read_text().splitlines(keepends=True)[:-1]))
    rebind(asset)
    fail = _failed(asset)
    assert fail.code == "V-SEAL-INCOMPLETE" and "prediction" in fail.detail


def test_cli_lists_the_skips(asset):
    r = runner.invoke(app, ["verify", f"rebases/{N}", "--asset", str(asset)])
    assert r.exit_code == 0, r.output
    for label in SKIPPED:
        assert label in r.output
    assert "verified" in r.output
    r = runner.invoke(app, ["verify", f"rebases/{N}", "--asset", str(asset), "--json"])
    data = json.loads(r.output)
    assert r.exit_code == 0 and data["ok"] is True
    assert sorted(c["label"] for c in data["checks"] if c["status"] == "skipped") == SKIPPED
    assert all(c["status"] in ("ok", "skipped") for c in data["checks"])


def test_cli_exit_4_names_the_check(asset):
    (asset / f"rebases/{N}/scores.json").write_text("{}\n")
    r = runner.invoke(app, ["verify", f"rebases/{N}", "--asset", str(asset), "--json"])
    data = json.loads(r.output)
    assert r.exit_code == 4 and data["failed"] == "V-HASH-MISMATCH" and "scores.json" in data["detail"]
