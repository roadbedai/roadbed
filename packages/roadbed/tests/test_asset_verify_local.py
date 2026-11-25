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
            "seal not after rebase", "seal precedes predictions: git ancestry", "train and holdout disjoint: exact",
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


def _relabelled_pass(tmp_path: Path, *, clone_it: bool = False) -> Path:
    """The control-plane pass fixture made to look local: rebase 13 without receipt, signature or evidence, the seal
    relabelled local, roadbed.lock deleted, gate.json recomputed as the engine gate, the manifest re-hashed. The asset
    still has policy/keys and versions/v12/receipt.json (and, in a clone, the lock in git history)."""
    from roadbed.asset.verify import _Ctx
    from roadbed.cli.asset import open_asset
    from roadbed.gate import decide_license, evaluate
    from test_asset_support import clone

    root = clone("pass", tmp_path / "a") if clone_it else copy("pass", tmp_path / "a")
    d = root / "rebases/13"
    for f in ("receipt.json", "receipt.sig"):
        (d / f).unlink()
    shutil.rmtree(d / "evidence")
    _localise(root, "data/holdout/H-2026-11-01/SEAL.json")
    (root / "roadbed.lock").unlink()
    a = open_asset(root)
    c = _Ctx(a, d, 13, None, False, None)
    lic_id = ((c.meta or {}).get("base") or {}).get("license")
    lic = decide_license(lic_id, a.license_policy) if lic_id and a.license_policy is not None else None
    (d / "gate.json").write_text(evaluate(c.scores(), None, lic, a.policy, finding_offset=0).model_dump_json(exclude_none=True, indent=2) + "\n")
    m = d / "MANIFEST.sha256"
    lines = [ln for ln in m.read_text().splitlines() if not ln.endswith(("receipt.json", "receipt.sig")) and "/evidence/" not in ln]
    m.write_text(_rehash(root, "\n".join(lines) + "\n"))
    return root


@pytest.mark.parametrize("clone_it", [False, True])
def test_control_plane_markers_are_never_local(tmp_path, clone_it):
    root = _relabelled_pass(tmp_path, clone_it=clone_it)
    d = root / "rebases/13"
    assert is_full(root, d) and not is_local(root, d)
    r = verify(d)
    assert r.failed is not None and r.failed.code == "V-HASH-MISSING", (r.failed and r.failed.code, r.failed and r.failed.detail)


def test_lock_in_git_history_is_never_local(asset):
    (asset / "roadbed.lock").write_text("{}\n")
    git(asset, "add", "roadbed.lock")
    git(asset, "commit", "-q", "-m", "a lock")
    git(asset, "rm", "-q", "roadbed.lock")
    git(asset, "commit", "-q", "-m", "drop the lock")
    assert not is_local(asset, asset / REBASE)


def test_an_email_in_rebase_yaml_fails_v_pii(asset):
    y = asset / REBASE / "rebase.yaml"
    y.write_text(y.read_text() + "note: call jane.doe@acme-mutual.com\n")
    _rehash_manifest(asset)
    git(asset, "commit", "-q", "-am", "an email")
    f = _failed(asset)
    assert f.code == "V-PII" and "rebase.yaml" in f.detail


def test_created_in_the_same_second_as_the_seal_passes(asset):
    import re

    sealed = json.loads((asset / SEAL).read_text())["sealed_at"]
    y = asset / REBASE / "rebase.yaml"
    y.write_text(re.sub(r"created_at: .*", f"created_at: '{sealed}'", y.read_text()))
    _rehash_manifest(asset)
    git(asset, "commit", "-q", "-am", "same second")
    r = _verify(asset)
    assert r.ok, r.failed
    assert any(c.label == "seal not after rebase" and "not after" in c.detail for c in r.checks)


def test_rebase_over_a_local_seal_says_local_not_attested(template):
    from roadbed.cli.rebase import final_line
    from roadbed.contracts import GateResult

    report = (template / REBASE / "report.md").read_text()
    assert f"rebase #{LOCAL_N}: gate pass, local, not attested; commit it, then roadbed verify {REBASE}" in report
    assert "MRM" not in report
    gate = GateResult.model_validate(json.loads((template / REBASE / "gate.json").read_text()))
    assert final_line(LOCAL_N, gate, -2.0) == f"opened rebase #{LOCAL_N} → awaiting MRM sign-off"  # control-plane seal


@pytest.mark.parametrize("rel", ["evals/scorers.yaml", "evals/golden.jsonl"])
def test_an_unflagged_file_or_row_fails_v_synthetic_flag(asset, rel):
    f = asset / rel
    if rel.endswith(".jsonl"):
        lines = f.read_text().splitlines()
        row = json.loads(lines[0])
        del row["synthetic"]
        f.write_text("\n".join([json.dumps(row), *lines[1:]]) + "\n")
    else:
        f.write_text(f.read_text().replace("synthetic: true\n", ""))
    git(asset, "commit", "-q", "-am", "drop a flag")
    fail = _failed(asset)
    assert fail.code == "V-SYNTHETIC-FLAG" and rel in fail.detail


def test_a_receipt_added_and_removed_only_in_merge_commits_is_never_local(asset):
    base = git(asset, "rev-parse", "--abbrev-ref", "HEAD")
    for op in ("add", "remove"):
        git(asset, "checkout", "-q", "-B", "side", base)
        (asset / "side.txt").write_text(op)
        git(asset, "add", "side.txt")
        git(asset, "commit", "-q", "-m", f"side {op}")
        git(asset, "checkout", "-q", base)
        git(asset, "merge", "-q", "--no-ff", "--no-commit", "side")
        if op == "add":
            (asset / "rebases/2").mkdir()
            (asset / "rebases/2/receipt.json").write_text("{}")
            git(asset, "add", "rebases/2/receipt.json")
        else:
            git(asset, "rm", "-q", "rebases/2/receipt.json")
        git(asset, "commit", "-q", "-m", f"merge {op}")
    assert "rebases/2/receipt.json" not in git(asset, "log", "--format=", "--name-only")  # merges list no files
    assert not is_local(asset, asset / REBASE)
    assert not _verify(asset).ok


def test_a_control_plane_file_in_history_is_named(asset):
    (asset / "roadbed.lock").write_text("{}\n")
    git(asset, "add", "roadbed.lock")
    git(asset, "commit", "-q", "-m", "a lock")
    git(asset, "rm", "-q", "roadbed.lock")
    git(asset, "commit", "-q", "-m", "drop the lock")
    f = _failed(asset)
    assert f.detail.startswith("local mode refused: roadbed.lock is in git history; "), f.detail


def test_a_control_plane_file_on_disk_is_named(asset):
    (asset / "policy/keys").mkdir()
    (asset / "policy/keys/receipt.pub").write_text("x\n")
    f = _failed(asset)
    assert f.detail.startswith("local mode refused: policy/keys/receipt.pub is in the asset; "), f.detail
