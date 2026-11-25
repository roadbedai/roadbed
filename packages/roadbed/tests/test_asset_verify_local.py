"""roadbed verify rebases/N for a local rebase: a local seal, predictions supplied, no receipts. Every guarantee a
laptop cannot give is a named skip, so --strict never passes; full and engine-scored rebases over a local seal fail
V-SEAL-ORDER (a receipt cannot bind a local seal)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from test_asset_verify_engine import engine_scored
from typer.testing import CliRunner

from roadbed.asset import build_lock, verify, write_lock
from roadbed.asset.hashing import merkle_root, sha256_bytes
from roadbed.asset.verify import is_engine_scored, is_full, is_local
from roadbed.cli.main import app
from test_asset_support import LOCAL_HID, LOCAL_N, _rehash, copy, edit_json, git, local_asset, reseal

cli = CliRunner()
REBASE = f"rebases/{LOCAL_N}"
SEAL = f"data/holdout/{LOCAL_HID}/SEAL.json"
SKIPPED = [
    "bom.cdx.json: skipped",
    "contamination.json matches: skipped",
    "evidence manifest OK: skipped",
    "license.json: skipped",
    "lock files re-hashed: skipped",
    "receipt chain OK: skipped",
    "receipts bind the predictions and the holdout: skipped",
    "receipts signed: skipped",
    "seal precedes training: audit seq: skipped",
    "segregation of duties (SoD-01, SoD-02): skipped",
]


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    return local_asset(tmp_path_factory.mktemp("local"), rebase=True)


@pytest.fixture
def asset(template, tmp_path) -> Path:
    shutil.copytree(template.parent / "objects", tmp_path / "objects")
    return Path(shutil.copytree(template, tmp_path / "acme"))


def _verify(root: Path, **kw):
    return verify(root / REBASE, objects=root.parent / "objects", **kw)


def _failed(root: Path, **kw):
    r = _verify(root, **kw)
    assert r.failed is not None, [(c.label, c.ok, c.detail) for c in r.checks]
    return r.failed


def _rehash_manifest(root: Path) -> None:
    m = root / REBASE / "MANIFEST.sha256"
    m.write_text(_rehash(root, m.read_text()))


def test_mode(asset):
    d = asset / REBASE
    assert is_full(asset, d) and is_local(asset, d) and not is_engine_scored(asset, d)


def test_passes_with_named_skips(asset):
    r = _verify(asset)
    assert r.ok, r.failed
    assert sorted(c.label for c in r.checks if c.ok is None) == SKIPPED
    passed = {c.label for c in r.checks if c.ok}
    assert {"hashes OK", "holdout re-hashed", "seal committed and unchanged", "MinHash object covers the sealed rows",
            "predictions cover the sealed holdout", "scores recomputed: match", "gate recomputed: match",
            "seal precedes rebase: OK", "seal precedes predictions: git ancestry", "train and holdout disjoint: exact",
            "train and holdout disjoint: near (Jaccard < 0.8)", "bucket objects re-hashed"} <= passed


def test_strict_fails_at_receipts_signed(asset):
    f = _failed(asset, strict=True)
    assert f.code == "V-SIG-MISSING" and f.label == "receipts signed" and "--strict" in f.detail


def test_seal_committed_after_the_predictions(asset, tmp_path):
    """Rewrite history: the predictions are committed before the seal."""
    root = tmp_path / "rewritten"
    shutil.copytree(asset, root, ignore=shutil.ignore_patterns(".git"))
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "--", f"{REBASE}/predictions.candidate.jsonl", f"{REBASE}/predictions.incumbent.jsonl")
    git(root, "commit", "-q", "-m", "predictions first")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seal and the rest")
    f = _failed(root)  # root.parent is tmp_path, which holds the objects copy
    assert f.code == "V-SEAL-ORDER" and "not a strict ancestor" in f.detail


def _plant_holdout_row(root: Path, *, rewrite: bool) -> None:
    """Copy a holdout row's hashes into the first snapshot row. With rewrite, re-hash the part and the manifest so
    only the overlap check can catch it."""
    hold = json.loads((root / f"data/holdout/{LOCAL_HID}/holdout.jsonl").read_text().splitlines()[0])
    man_rel = "data/snapshots/2026-11-02/MANIFEST.json"
    man = json.loads((root / man_rel).read_text())
    part = root / man["parts"][0]["path"]
    rows = [json.loads(x) for x in part.read_text().splitlines()]
    rows[0] = {**rows[0], "input_ref": hold["input_ref"]}
    if rewrite:
        from roadbed.asset import row_hash

        rows[0]["row_hash"] = row_hash(rows[0])
    data = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows).encode()
    part.write_bytes(data)
    if rewrite:
        man["parts"][0].update(oid=sha256_bytes(data), size=len(data))
        hashes = [r["row_hash"] for r in rows] + [json.loads(x)["row_hash"] for p in man["parts"][1:]
                                                   for x in (root / p["path"]).read_text().splitlines()]
        man["merkle_root"] = merkle_root(hashes)
        (root / man_rel).write_text(json.dumps(man, indent=2) + "\n")
        _rehash_manifest(root)


def test_holdout_row_in_the_snapshot_rewritten(asset):
    shutil.rmtree(asset / ".git")  # the snapshot changes after the seal commit; without a clone the overlap check decides
    _plant_holdout_row(asset, rewrite=True)
    f = _failed(asset)
    assert f.code == "V-HOLDOUT-OVERLAP" and "inputs in both" in f.detail


def test_holdout_row_in_the_snapshot_not_rewritten(asset):
    _plant_holdout_row(asset, rewrite=False)
    f = _failed(asset)
    assert f.code == "V-HASH-MISMATCH"


def test_sealed_after_the_rebase_was_created(asset):
    shutil.rmtree(asset / ".git")
    edit_json(asset / SEAL, lambda d: d.update(sealed_at="2030-01-01T00:00:00Z"))
    _rehash_manifest(asset)
    f = _failed(asset)
    assert f.code == "V-SEAL-AFTER-REBASE"


def test_tampered_scores(asset):
    edit_json(asset / REBASE / "scores.json", lambda d: d["overall"].update(candidate=d["overall"]["candidate"] - 0.5))
    _rehash_manifest(asset)
    f = _failed(asset)
    assert f.code == "V-SCORES-MISMATCH"


def test_control_plane_fixture_stripped_of_receipts_stays_full(tmp_path):
    root = copy("pass", tmp_path / "a")
    d = root / "rebases/13"
    for f in ("receipt.json", "receipt.sig"):
        (d / f).unlink()
    shutil.rmtree(d / "evidence")
    assert is_full(root, d) and not is_local(root, d) and not is_engine_scored(root, d)
    r = verify(d)
    assert r.failed is not None and r.failed.code == "V-HASH-MISSING" and "receipt.json" in r.failed.detail


def _localise(root: Path, seal_rel: str) -> None:
    def f(doc: dict) -> None:
        del doc["audit_seq"]
        doc["provenance"] = "local"

    edit_json(root / seal_rel, f)


def test_full_rebase_over_a_local_seal_fails_seal_order(tmp_path):
    root = copy("pass", tmp_path / "a")
    _localise(root, "data/holdout/H-2026-11-01/SEAL.json")
    reseal(root, 13)
    assert not is_local(root, root / "rebases/13")
    r = verify(root / "rebases/13")
    assert r.failed is not None and r.failed.code == "V-SEAL-ORDER", (r.failed.code, r.failed.detail)
    assert "cannot bind a local seal" in r.failed.detail


def test_engine_scored_rebase_over_a_local_seal_fails_seal_order(tmp_path):
    root = engine_scored(tmp_path / "a")
    _localise(root, "data/holdout/H-2026-11-01/SEAL.json")
    m = root / "rebases/13/MANIFEST.sha256"
    m.write_text(_rehash(root, m.read_text()))
    write_lock(root, build_lock(root, previous=json.loads((root / "roadbed.lock").read_text()), synthetic=True))
    assert is_engine_scored(root, root / "rebases/13") and not is_local(root, root / "rebases/13")
    r = verify(root / "rebases/13")
    assert r.failed is not None and r.failed.code == "V-SEAL-ORDER", (r.failed.code, r.failed.detail)
    assert "cannot bind a local seal" in r.failed.detail


def test_cli_local_rebase(asset):
    objects = str(asset.parent / "objects")
    r = cli.invoke(app, ["verify", REBASE, "--asset", str(asset), "--objects", objects])
    assert r.exit_code == 0, r.output
    for label in SKIPPED:
        assert label in r.output
    assert r.output.rstrip().endswith("LOCAL: no run was attested; sealed by vik; no audit chain; no role enforcement")
    r = cli.invoke(app, ["verify", REBASE, "--asset", str(asset), "--objects", objects, "--json"])
    o = json.loads(r.output)
    assert r.exit_code == 0 and o["ok"] and o["local"] is True
    assert sorted(c["label"] for c in o["checks"] if c["status"] == "skipped") == SKIPPED
    r = cli.invoke(app, ["verify", REBASE, "--asset", str(asset), "--objects", objects, "--strict", "--json"])
    o = json.loads(r.output)
    assert r.exit_code == 4 and o["failed"] == "V-SIG-MISSING" and o["local"] is True
