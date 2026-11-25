"""roadbed verify data/holdout/<id>: the checks of one sealed holdout, for a local seal and a control-plane seal."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from roadbed.asset import ObjectStore, VerifyFailed
from roadbed.asset.overlap import minhash_object, parse_minhash_object
from roadbed.asset.verify import run_seal
from roadbed.cli.asset import open_asset
from roadbed.cli.main import app
from test_asset_support import LOCAL_HID, bucket, clone, copy, edit_json, git, local_asset

cli = CliRunner()
HDIR = f"data/holdout/{LOCAL_HID}"
NO_OBJECTS = ["MinHash object covers the sealed rows: skipped", "train and holdout disjoint: near (Jaccard < 0.8): skipped"]


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    return local_asset(tmp_path_factory.mktemp("local"))


@pytest.fixture
def asset(template, tmp_path) -> Path:
    """A fresh copy of the local asset (with its git history) and of its object store."""
    shutil.copytree(template.parent / "objects", tmp_path / "objects")
    return Path(shutil.copytree(template, tmp_path / "acme"))


def _run(root: Path, hdir: str = HDIR, *, objects: bool = True, strict: bool = False):
    """(checks, failed check or None)."""
    checks: list = []
    try:
        run_seal(open_asset(root), root / hdir, checks.append, objects=root.parent / "objects" if objects else None, strict=strict)
    except VerifyFailed as e:
        return checks, e.check
    return checks, None


def _skips(checks) -> list[str]:
    return sorted(c.label for c in checks if c.ok is None)


def test_clone_and_objects_pass_strict_with_zero_skips(asset):
    checks, failed = _run(asset, strict=True)
    assert failed is None, failed
    assert _skips(checks) == [] and len(checks) == 11
    assert {"V-SCHEMA-INVALID", "V-HASH-MISMATCH", "V-PII", "V-SYNTHETIC-FLAG", "V-SEAL-ORDER", "V-HOLDOUT-OVERLAP"} <= {c.code for c in checks}


def test_without_objects_two_named_skips(asset):
    checks, failed = _run(asset, objects=False)
    assert failed is None, failed
    assert _skips(checks) == NO_OBJECTS
    checks, failed = _run(asset, objects=False, strict=True)
    assert failed is not None and failed.code == "V-OBJECT-MISSING" and "--strict" in failed.detail


def test_without_a_clone_the_seal_commit_is_a_named_skip(asset):
    shutil.rmtree(asset / ".git")
    checks, failed = _run(asset)
    assert failed is None, failed
    assert _skips(checks) == ["seal committed and unchanged: skipped"]


def test_flipped_byte_in_the_holdout(asset):
    f = asset / HDIR / "holdout.jsonl"
    b = bytearray(f.read_bytes())
    b[len(b) // 2] ^= 0x01
    f.write_bytes(bytes(b))
    _, failed = _run(asset)
    assert failed is not None and failed.code == "V-HASH-MISMATCH" and "holdout.jsonl" in failed.detail


def test_seal_edited_after_the_commit(asset):
    edit_json(asset / HDIR / "SEAL.json", lambda d: d.update(sealed_at="2026-11-01T00:00:00Z"))
    _, failed = _run(asset)
    assert failed is not None and failed.code == "V-SEAL-ORDER" and "differs from the committed file" in failed.detail


def test_holdout_row_copied_into_golden(asset):
    row = (asset / HDIR / "holdout.jsonl").read_text().splitlines()[0]
    with open(asset / "evals/golden.jsonl", "a") as f:
        f.write(row + "\n")
    _, failed = _run(asset)
    assert failed is not None and failed.code == "V-HOLDOUT-OVERLAP" and "golden or correction tests" in failed.detail


def test_minhash_object_missing_one_row(asset):
    shutil.rmtree(asset / ".git")  # SEAL.json changes below; without a clone only the MinHash check can catch it
    store = ObjectStore(asset.parent / "objects")
    seal = json.loads((asset / HDIR / "SEAL.json").read_text())
    n, k, sigs = parse_minhash_object(store.get(seal["minhash_oid"]))
    sigs.pop(sorted(sigs)[0])
    oid = store.put(minhash_object(sigs, n, k))
    edit_json(asset / HDIR / "SEAL.json", lambda d: d.update(minhash_oid=oid))
    _, failed = _run(asset)
    assert failed is not None and failed.code == "V-HOLDOUT-OVERLAP" and "exactly the sealed holdout rows" in failed.detail


def test_holdout_row_in_the_snapshot_fails_exact(asset):
    shutil.rmtree(asset / ".git")
    hold = json.loads((asset / HDIR / "holdout.jsonl").read_text().splitlines()[0])
    man_rel = "data/snapshots/2026-11-02/MANIFEST.json"
    man = json.loads((asset / man_rel).read_text())
    part = asset / man["parts"][0]["path"]
    rows = [json.loads(x) for x in part.read_text().splitlines()]
    rows[0] = {**rows[0], "row_hash": hold["row_hash"]}  # a tampered row: verify re-hashes it
    part.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
    _, failed = _run(asset)
    assert failed is not None and failed.code == "V-HASH-MISMATCH" and "snapshot part" in failed.detail


def test_control_plane_seal_takes_the_same_entry_point(tmp_path):
    root = clone("pass", tmp_path / "pass")
    bucket(tmp_path / "objects")
    checks, failed = _run(root, "data/holdout/H-2026-11-01")
    assert failed is None, failed
    assert checks[1].ok is None and checks[1].label == "seal audit seq: skipped"
    assert checks[1].detail == "not checked here: a receipt binds audit seq 1204 at rebase time"
    assert _skips(checks) == ["datasets re-hashed: skipped", "seal audit seq: skipped", "train and holdout disjoint: exact: skipped",
                              "train and holdout disjoint: near (Jaccard < 0.8): skipped"]
    _, failed = _run(root, "data/holdout/H-2026-11-01", strict=True)
    assert failed is not None and failed.label == "seal audit seq" and "--strict" in failed.detail


def test_cli_seal_mode(asset):
    r = cli.invoke(app, ["verify", HDIR, "--asset", str(asset), "--objects", str(asset.parent / "objects"), "--strict"])
    assert r.exit_code == 0, r.output
    assert f"verified seal {LOCAL_HID}" in r.output
    assert r.output.rstrip().endswith("LOCAL seal: sealed by vik; no audit chain; no role enforcement")
    r = cli.invoke(app, ["verify", str(asset / HDIR), "--json"])
    o = json.loads(r.output)
    assert r.exit_code == 0 and o["ok"] and o["local"] is True and o["holdout"] == LOCAL_HID
    assert sorted(c["label"] for c in o["checks"] if c["status"] == "skipped") == NO_OBJECTS


def test_cli_seal_mode_failure_exits_4(asset):
    (asset / HDIR / "holdout.jsonl").write_text("{}\n")
    r = cli.invoke(app, ["verify", HDIR, "--asset", str(asset), "--json"])
    o = json.loads(r.output)
    assert r.exit_code == 4 and o["failed"] == "V-HASH-MISMATCH"


def test_a_seal_copied_into_a_rebase_dir_does_not_switch_to_seal_mode(tmp_path):
    root = copy("pass", tmp_path / "pass")
    edit_json(root / "rebases/13/scores.json", lambda s: s["overall"].update(candidate=s["overall"]["candidate"] + 20))
    for f in ("SEAL.json", "holdout.jsonl"):
        shutil.copy(root / "data/holdout/H-2026-11-01" / f, root / "rebases/13" / f)
    r = cli.invoke(app, ["verify", "rebases/13", "--asset", str(root), "--json"])
    o = json.loads(r.output)
    assert r.exit_code == 4 and o["failed"] == "V-HASH-MISMATCH" and "scores.json" in o["detail"] and "rebase" in o


def test_seal_mode_only_under_data_holdout(asset, tmp_path):
    other = asset / "evals/H-copy"
    shutil.copytree(asset / HDIR, other)
    r = cli.invoke(app, ["verify", "evals/H-copy", "--asset", str(asset)])
    assert r.exit_code == 2 and "verified seal" not in r.output


@pytest.mark.parametrize("damage,code", [("corrupt", "V-SCHEMA-INVALID"), ("missing", "V-HASH-MISSING")])
def test_cli_bad_seal_file_exits_4_and_names_it(asset, damage, code):
    f = asset / HDIR / "SEAL.json"
    if damage == "corrupt":
        f.write_text('{"id": "H-2026-11-02",')
    else:
        f.unlink()
    r = cli.invoke(app, ["verify", HDIR, "--asset", str(asset), "--json"])
    o = json.loads(r.output)
    assert r.exit_code == 4 and o["failed"] == code and f"{HDIR}/SEAL.json" in o["detail"]


def test_minhash_object_not_in_the_given_store(asset):
    oid = json.loads((asset / HDIR / "SEAL.json").read_text())["minhash_oid"]
    ObjectStore(asset.parent / "objects").path(oid).unlink()
    checks, failed = _run(asset)
    assert failed is None
    skip = next(c for c in checks if c.label == "MinHash object covers the sealed rows: skipped")
    assert skip.detail == f"MinHash object {oid[:12]} is not in --objects"
    skip = next(c for c in _run(asset, objects=False)[0] if c.label == "MinHash object covers the sealed rows: skipped")
    assert skip.detail == f"MinHash object {oid[:12]} not checked; pass --objects"


def test_seal_mode_checks_every_synthetic_flag(asset):
    f = asset / "evals/golden.jsonl"
    lines = f.read_text().splitlines()
    row = json.loads(lines[0])
    del row["synthetic"]
    f.write_text("\n".join([json.dumps(row), *lines[1:]]) + "\n")
    _, failed = _run(asset)
    assert failed is not None and failed.code == "V-SYNTHETIC-FLAG" and "evals/golden.jsonl" in failed.detail


def test_seal_mode_refuses_flagged_rows_in_a_real_asset(asset):
    y = asset / "roadbed.yaml"
    y.write_text(y.read_text().replace("synthetic: true", "synthetic: false"))
    _, failed = _run(asset)
    assert failed is not None and failed.code == "V-SYNTHETIC-FLAG" and "roadbed.yaml does not" in failed.detail


REBASE_FILES = ("scores.json", "gate.json", "rebase.yaml", "predictions.candidate.jsonl", "predictions.incumbent.jsonl",
                "MANIFEST.sha256", "report.md")


@pytest.fixture(scope="module")
def rebased(tmp_path_factory) -> Path:
    return local_asset(tmp_path_factory.mktemp("rebased"), rebase=True)


def test_a_symlinked_rebase_dir_never_reaches_seal_mode(rebased, tmp_path):
    """Rebase files with +20 on the score inside the holdout dir, and a committed symlink rebases/2 to it."""
    shutil.copytree(rebased.parent / "objects", tmp_path / "objects")
    root = Path(shutil.copytree(rebased, tmp_path / "acme", symlinks=True))
    for f in REBASE_FILES:
        shutil.copy(root / "rebases/1" / f, root / HDIR / f)
    edit_json(root / HDIR / "scores.json", lambda s: s["overall"].update(candidate=s["overall"]["candidate"] + 20))
    (root / "rebases/2").symlink_to(f"../{HDIR}")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "rebase 2")
    r = cli.invoke(app, ["verify", "rebases/2", "--asset", str(root), "--objects", str(tmp_path / "objects"), "--strict"])
    assert r.exit_code != 0 and "verified" not in r.output, r.output


def test_a_symlinked_holdout_dir_is_not_seal_mode(asset):
    (asset / "data/holdout/H-link").symlink_to(LOCAL_HID)
    r = cli.invoke(app, ["verify", "data/holdout/H-link", "--asset", str(asset), "--objects", str(asset.parent / "objects")])
    assert r.exit_code == 2 and "verified" not in r.output and "rebase directory must be named by its number" in r.output, r.output
