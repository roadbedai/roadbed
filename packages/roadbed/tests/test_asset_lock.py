"""Lock: build reproduces the fixture locks byte for byte; check finds every drift."""

from __future__ import annotations

import json
import os

import pytest

from roadbed.asset import build_lock, check_lock, write_lock
from test_asset_support import EXAMPLES, bucket, copy


@pytest.mark.parametrize("asset", ["pass", "blocked"])
def test_build_lock_reproduces_fixture(asset, tmp_path):
    root = copy(asset, tmp_path / asset)
    stored = (root / "roadbed.lock").read_bytes()
    prev = json.loads(stored)
    write_lock(root, build_lock(root, previous=prev, synthetic=True))
    assert (root / "roadbed.lock").read_bytes() == stored


def test_build_lock_takes_sizes_from_the_bucket(tmp_path):
    """Without the previous lock, sizes of objects the tree does not size come from the bucket (MinHash objects);
    the adapters are sized in model.json, the receipt's adapter only in the previous lock."""
    store = bucket(tmp_path / "bucket")
    prev = json.loads((EXAMPLES / "pass/roadbed.lock").read_text())
    adapters = {"objects": [o for o in prev["objects"] if o["size"] == 66_060_288]}
    doc = build_lock(EXAMPLES / "pass", store=store, previous=adapters, synthetic=True)
    assert doc == prev


@pytest.mark.parametrize("asset", ["pass", "blocked"])
def test_check_lock_passes(asset, tmp_path):
    problem, nfiles, nobj = check_lock(EXAMPLES / asset, store=bucket(tmp_path / "b"))
    assert problem is None and nfiles > 40 and nobj == 3  # 2 MinHash objects and 1 snapshot part re-hashed


def test_flipped_byte(tmp_path):
    root = copy("pass", tmp_path / "a")
    f = root / "lineage/edges.jsonl"
    b = bytearray(f.read_bytes())
    b[10] ^= 1
    f.write_bytes(bytes(b))
    problem, _, _ = check_lock(root)
    assert problem and problem.code == "V-HASH-MISMATCH" and "lineage/edges.jsonl" in problem.detail


def test_unlisted_file(tmp_path):
    root = copy("pass", tmp_path / "a")
    (root / "data/holdout/H-2026-11-01/extra.jsonl").write_text("{}\n")
    problem, _, _ = check_lock(root)
    assert problem and problem.code == "V-LOCK-INCOMPLETE" and "extra.jsonl" in problem.detail


def test_missing_file(tmp_path):
    root = copy("pass", tmp_path / "a")
    (root / "findings/F-0001.yaml").unlink()
    problem, _, _ = check_lock(root)
    assert problem and problem.code == "V-HASH-MISSING"


def test_unlisted_object(tmp_path):
    root = copy("pass", tmp_path / "a")
    lock = json.loads((root / "roadbed.lock").read_text())
    lock["objects"] = [o for o in lock["objects"] if "SEAL" not in o["ref"]]
    (root / "roadbed.lock").write_text(json.dumps(lock))
    problem, _, _ = check_lock(root)
    assert problem and problem.code == "V-LOCK-INCOMPLETE" and "SEAL.json" in problem.detail


def test_object_bytes_must_match(tmp_path):
    store = bucket(tmp_path / "b")
    lock = json.loads((EXAMPLES / "pass/roadbed.lock").read_text())
    oid = lock["objects"][0]["oid"]
    store.path(oid).write_bytes(b"tampered")
    problem, _, _ = check_lock(EXAMPLES / "pass", store=store)
    assert problem and problem.code == "V-HASH-MISMATCH"


def test_path_outside_asset(tmp_path):
    root = copy("pass", tmp_path / "a")
    lock = json.loads((root / "roadbed.lock").read_text())
    lock["files"].insert(0, {"path": "../outside", "sha256": "0" * 64, "size": 0})
    (root / "roadbed.lock").write_text(json.dumps(lock))
    problem, _, _ = check_lock(root)
    assert problem and problem.code == "V-PATH-UNSAFE"


def test_symlink_refused(tmp_path):
    root = copy("pass", tmp_path / "a")
    target = root / "findings/F-0001.yaml"
    moved = tmp_path / "elsewhere.yaml"
    target.rename(moved)
    os.symlink(moved, target)
    problem, _, _ = check_lock(root)
    assert problem and problem.code == "V-PATH-UNSAFE"
