"""Verify: passes on both fixture assets, and each tamper fails with its V-* code.

Most tampers go through reseal(): the attacker holds the signing keys and makes every hash, manifest, signature and
the lock consistent again, so only the check under test can catch the change.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from roadbed.asset import build_lock, read_doc, row_hash, verify, write_lock
from roadbed.asset.hashing import jcs_sha256
from roadbed.asset.overlap import minhash_object, parse_minhash_object
from roadbed.asset.verify import _config, _Ctx
from roadbed.cli.asset import open_asset
from roadbed.cli.main import app
from roadbed.contracts import GatePolicy
from test_asset_support import EXAMPLES, REBASE, bucket, builder, clone, copy, edit_json, git, reseal

runner = CliRunner()
KEYS = {"cb54bac00a078e10310f8766a331895c9c69a8e4350ea81af133f66ea84955b0", "3117f7ad8a2325c38a84077422c2fff4c442a9378c75ec168529399b99d938cd"}
SNAP = "data/snapshots/2026-11-01"
HOLD = "data/holdout/H-2026-11-01"


@pytest.fixture(scope="module")
def store(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("bucket")
    bucket(p)
    return p


def _failed(root: Path, n: int = 13, **kw):
    r = verify(root / f"rebases/{n}", **kw)
    assert r.failed is not None, [(c.code, c.ok, c.detail) for c in r.checks]
    return r.failed


# ---------------------------------------------------------------- passing


@pytest.mark.parametrize("asset", ["pass", "blocked"])
def test_passes_in_place(asset):
    """In the spec repo the fixtures have no git history of their own and no bucket: those checks are skipped. The
    working tree lock lists the receipt, so it is not the lock the job started from: config.lock_sha256 is skipped."""
    r = verify(EXAMPLES / asset / f"rebases/{REBASE[asset]}")
    assert r.ok and r.full
    skipped = sorted(c.label for c in r.checks if c.ok is None)
    assert skipped == ["bucket objects re-hashed: skipped", "receipt binds its config: skipped",
                       "seal precedes training: git ancestry: skipped",
                       "train and holdout disjoint: near (Jaccard < 0.8): skipped"]


@pytest.mark.parametrize("asset", ["pass", "blocked"])
def test_passes_strict_on_bundle_clone(asset, tmp_path, store):
    """The real history and the bucket: every check runs, closed schemas, pinned keys. The fixtures do not ship
    adapter weights (their oids are stand-ins, spec/tools/build_examples.py oid()), so the last check, bucket objects,
    fails under --strict naming exactly the two adapters; every check before it passes."""
    root = clone(asset, tmp_path / asset)
    n = REBASE[asset]
    r = verify(root / f"rebases/{n}", objects=store, strict=True, trusted_keyids=KEYS)
    assert all(c.ok is True for c in r.checks[:-1]), r.failed
    adapters = [builder().oid("claims-triage v12 adapter"), builder().oid(f"claims-triage rebase {n} adapter")]
    assert r.failed is r.checks[-1] and r.failed.code == "V-OBJECT-MISSING"
    assert r.failed.detail.startswith("2 of ") and all(a[:12] in r.failed.detail for a in adapters)
    codes = {c.code for c in r.checks}
    assert {"V-SEAL-ORDER", "V-HOLDOUT-OVERLAP", "V-SIG-INVALID", "V-RECEIPT-CHAIN", "V-LOCK-INCOMPLETE"} <= codes
    near = next(c for c in r.checks if c.label.startswith("train and holdout disjoint: near"))
    assert near.detail == "jaccard, max 0.02381"  # known answer: contamination.json max_jaccard


def test_minhash_fallback(tmp_path):
    """Without the raw holdout inputs, near overlap uses the sealed MinHash object."""
    root = clone("pass", tmp_path / "pass")
    b = bucket(tmp_path / "b", holdout_raw=False)
    r = verify(root / "rebases/13", objects=b.root, strict=True)
    assert all(c.ok is True for c in r.checks[:-1]), r.failed
    assert r.failed is r.checks[-1] and r.failed.code == "V-OBJECT-MISSING"  # raw holdout inputs and adapters
    assert any(c.detail.startswith("minhash, max") for c in r.checks)


def test_strict_fails_on_skips():
    f = _failed(EXAMPLES / "pass", strict=True)
    assert f.code == "V-SEAL-ORDER" and "--strict" in f.detail


def test_cli_exit_4_names_the_check(tmp_path, store):
    root = clone("pass", tmp_path / "pass")
    r = runner.invoke(app, ["verify", "rebases/13", "--asset", str(root), "--strict", "--objects", str(store), "--json"])
    assert r.exit_code == 4 and json.loads(r.output)["failed"] == "V-OBJECT-MISSING", r.output  # stand-in adapters
    r = runner.invoke(app, ["verify", "rebases/13", "--asset", str(root), "--objects", str(store), "--json"])
    assert r.exit_code == 0, r.output
    (root / "rebases/13/report.md").write_text("tampered\n")
    r = runner.invoke(app, ["verify", "rebases/13", "--asset", str(root), "--json"])
    data = json.loads(r.output)
    assert r.exit_code == 4 and data["failed"] == "V-HASH-MISMATCH" and "report.md" in data["detail"]


# ---------------------------------------------------------------- flipped bytes


@pytest.mark.parametrize("rel", [
    "rebases/13/predictions.candidate.jsonl",  # in the rebase manifest
    f"{HOLD}/holdout.jsonl",  # bound into the rebase manifest
    "lineage/edges.jsonl",  # only in the lock
    "rebases/13/evidence/approvals.json",  # in the evidence manifest
    "policy/keys/receipt.pub",  # only in the lock
])
def test_flipped_byte(tmp_path, rel):
    root = copy("pass", tmp_path / "a")
    f = root / rel
    b = bytearray(f.read_bytes())
    b[len(b) // 2] ^= 0x01
    f.write_bytes(bytes(b))
    f_ = _failed(root)
    assert f_.code == "V-HASH-MISMATCH" and rel in f_.detail


def test_flipped_byte_in_bucket_object(tmp_path):
    b = bucket(tmp_path / "b")
    part = read_doc(EXAMPLES / "pass/data/snapshots/2026-10-15/MANIFEST.json")["parts"][0]["oid"]
    data = bytearray(b.path(part).read_bytes())
    data[100] ^= 0x01
    b.path(part).write_bytes(bytes(data))
    assert _failed(EXAMPLES / "pass", objects=b.root).code == "V-HASH-MISMATCH"


def test_resealed_snapshot_row_is_caught_by_row_hash(tmp_path):
    """Change a training verdict and re-hash every file: the stored row_hash no longer matches the row."""
    root = copy("pass", tmp_path / "a")
    part = root / f"{SNAP}/part-0000.jsonl"
    rows = read_doc(part)
    rows[0]["verdict"]["severity"] = 5
    part.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
    _fix_snapshot(root, rows, rehash_rows=False)
    reseal(root, 13)
    f = _failed(root)
    assert f.code == "V-HASH-MISMATCH" and "row_hash does not match" in f.detail


# ---------------------------------------------------------------- seal order


def test_seal_after_training_by_audit_seq(tmp_path):
    root = copy("pass", tmp_path / "a")
    edit_json(root / f"{HOLD}/SEAL.json", lambda d: d.update(audit_seq=1400))
    edit_json(root / "rebases/13/receipt.json", lambda d: d["predicate"]["holdout"].update(seal_seq=1400))
    reseal(root, 13, chain=True)
    f = _failed(root)
    assert f.code == "V-SEAL-ORDER" and "1400" in f.detail


def test_seal_after_training_by_time(tmp_path):
    root = copy("pass", tmp_path / "a")
    late = "2026-11-01T10:30:00Z"  # training started 10:20
    edit_json(root / f"{HOLD}/SEAL.json", lambda d: d.update(sealed_at=late))
    edit_json(root / "rebases/13/receipt.json", lambda d: d["predicate"]["holdout"].update(sealed_at=late))
    meta = root / "rebases/13/rebase.yaml"
    meta.write_text(meta.read_text().replace("2026-11-01T10:14:00Z", "2026-11-01T10:40:00Z"))  # keep the Day 0 leg quiet
    root.joinpath("roadbed.yaml").write_text(root.joinpath("roadbed.yaml").read_text().replace("2026-11-01T09:02:00Z", late))
    reseal(root, 13, chain=True)
    f = _failed(root)
    assert f.code == "V-SEAL-ORDER" and "is not before training started" in f.detail


def test_seal_committed_after_training(tmp_path, store):
    """Rewrite history: the receipt is committed before the seal. Seqs and times still look right."""
    src = clone("pass", tmp_path / "src")
    root = tmp_path / "rewritten"
    shutil.copytree(src, root, ignore=shutil.ignore_patterns(".git"))
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "--", "rebases/13/receipt.json")
    git(root, "commit", "-q", "-m", "train first")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seal and the rest")
    f = _failed(root, objects=store)
    assert f.code == "V-SEAL-ORDER" and "not a strict ancestor" in f.detail


def test_seal_re_added_after_training(tmp_path, store):
    root = clone("pass", tmp_path / "pass")
    seal = root / f"{HOLD}/SEAL.json"
    data = seal.read_bytes()
    git(root, "rm", "-q", f"{HOLD}/SEAL.json")
    git(root, "commit", "-q", "-m", "drop seal")
    seal.write_bytes(data)
    git(root, "add", f"{HOLD}/SEAL.json")
    git(root, "commit", "-q", "-m", "re-add seal")
    f = _failed(root, objects=store)
    assert f.code == "V-SEAL-ORDER" and "re-added" in f.detail


def test_uncommitted_seal_edit(tmp_path, store):
    root = clone("pass", tmp_path / "pass")
    edit_json(root / f"{HOLD}/SEAL.json", lambda d: d.update(seed=1))
    reseal(root, 13)
    f = _failed(root, objects=store)
    assert f.code == "V-SEAL-ORDER" and "differs from the committed file" in f.detail


def test_receipt_seal_commit_must_be_git_s(tmp_path, store):
    root = clone("pass", tmp_path / "pass")
    edit_json(root / "rebases/13/receipt.json", lambda d: d["predicate"]["holdout"].update(seal_commit="a" * 40))
    reseal(root, 13, chain=True, commit="forge seal_commit")  # commits the receipt again: it was changed after training
    f = _failed(root, objects=store)
    assert f.code == "V-SEAL-ORDER"


# ---------------------------------------------------------------- disjointness


def _fix_snapshot(root: Path, rows: list[dict], *, rehash_rows: bool = True) -> None:
    """Rewrite the training part, its manifest and the receipt's snapshot reference after a change to the rows."""
    from roadbed.asset import merkle_root, sha256_bytes

    if rehash_rows:
        for r in rows:
            r["row_hash"] = row_hash(r)
    data = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows).encode()
    (root / f"{SNAP}/part-0000.jsonl").write_bytes(data)
    root_ = merkle_root(r["row_hash"] for r in rows)

    def man(d):
        d.update(rows=len(rows), merkle_root=root_)
        d["parts"][0].update(oid=sha256_bytes(data), size=len(data), rows=len(rows))

    edit_json(root / f"{SNAP}/MANIFEST.json", man)
    edit_json(root / "rebases/13/receipt.json", lambda d: d["predicate"]["snapshot"].update(merkle_root=root_, rows=len(rows)))
    for f in ("contamination.json",):
        edit_json(root / f"rebases/13/{f}", lambda d: d["train"].update(merkle_root=root_, rows=len(rows)))


def _holdout_source_row(i: int) -> dict:
    b = builder()
    return b.verdict_row("h", i, "bodily_injury" if i < 30 else b.OTHER_LINES[i % 2])


def test_shared_row(tmp_path):
    """A holdout row's source verdict row goes into training: same row hash."""
    root = copy("pass", tmp_path / "a")
    rows = read_doc(root / f"{SNAP}/part-0000.jsonl") + [_holdout_source_row(0)]
    _fix_snapshot(root, rows)
    reseal(root, 13, chain=True)
    f = _failed(root)
    assert f.code == "V-HOLDOUT-OVERLAP" and "row hashes in both" in f.detail


def test_shared_input_under_a_new_row_hash(tmp_path):
    """The same input, re-labelled with a new timestamp: the row hash differs, the content address does not."""
    root = copy("pass", tmp_path / "a")
    row = {**_holdout_source_row(5), "ts": "2026-10-30T12:00:00Z", "interaction_id": "int-t-9999"}
    rows = read_doc(root / f"{SNAP}/part-0000.jsonl") + [row]
    _fix_snapshot(root, rows)
    reseal(root, 13, chain=True)
    f = _failed(root)
    assert f.code == "V-HOLDOUT-OVERLAP" and "same input_ref" in f.detail


@pytest.mark.parametrize("holdout_raw", [True, False], ids=["exact-jaccard", "minhash"])
def test_near_duplicate_input(tmp_path, holdout_raw):
    """A holdout input with its last word changed: a new content address, Jaccard 21/23 = 0.913 over 3-shingles."""
    b = builder()
    store = bucket(tmp_path / "b", holdout_raw=holdout_raw)
    words = b.narrative("h", 7).split()
    words[-1] = "zebra"
    text = " ".join(words)
    oid = store.put(text.encode())
    root = copy("pass", tmp_path / "a")
    row = {**_holdout_source_row(7), "input_ref": f"sha256:{oid}", "ts": "2026-10-30T12:00:00Z", "interaction_id": "int-t-9998"}
    rows = read_doc(root / f"{SNAP}/part-0000.jsonl") + [row]
    _fix_snapshot(root, rows)
    reseal(root, 13, chain=True)
    assert verify(root / "rebases/13").ok  # without the bucket the near check is skipped, not passed
    assert any(c.ok is None and "near" in c.label for c in verify(root / "rebases/13").checks)
    f = _failed(root, objects=store.root)
    assert f.code == "V-HOLDOUT-OVERLAP" and "near pairs" in f.detail
    assert ("jaccard 0.913043" if holdout_raw else "minhash") in f.detail


def test_minhash_object_must_cover_the_holdout(tmp_path):
    store = bucket(tmp_path / "b", holdout_raw=False)
    seal = read_doc(EXAMPLES / f"pass/{HOLD}/SEAL.json")
    n, k, sigs = parse_minhash_object(store.get(seal["minhash_oid"]))
    sigs.pop(next(iter(sigs)))
    forged = store.put(minhash_object(sigs, n, k))
    root = copy("pass", tmp_path / "a")
    edit_json(root / f"{HOLD}/SEAL.json", lambda d: d.update(minhash_oid=forged))
    reseal(root, 13, store=store)
    f = _failed(root, objects=store.root)
    assert f.code == "V-HOLDOUT-OVERLAP" and "does not cover" in f.detail


# ---------------------------------------------------------------- receipts, evidence, Day 0 path


def test_receipt_names_a_missing_file(tmp_path):
    root = copy("pass", tmp_path / "a")
    edit_json(root / "rebases/13/receipt.json",
              lambda d: d["subject"].append({"name": "rebases/13/adapter.safetensors", "digest": {"sha256": "0" * 64}}))
    reseal(root, 13, chain=True)
    f = _failed(root)
    assert f.code == "V-HASH-MISSING" and "adapter.safetensors" in f.detail


def test_manifest_names_a_missing_file(tmp_path):
    root = copy("pass", tmp_path / "a")
    (root / "rebases/13/report.md").unlink()
    f = _failed(root)
    assert f.code == "V-HASH-MISSING" and "report.md" in f.detail


def test_evidence_names_a_missing_file(tmp_path):
    root = copy("pass", tmp_path / "a")
    m = root / "rebases/13/evidence/MANIFEST.sha256"
    m.write_text(m.read_text() + "0" * 64 + "  rebases/13/evidence/evidence.pdf\n")
    reseal(root, 13)
    f = _failed(root)
    assert f.code == "V-HASH-MISSING" and "evidence.pdf" in f.detail


def test_evidence_must_list_its_files(tmp_path):
    root = copy("pass", tmp_path / "a")
    (root / "rebases/13/evidence/evidence.pdf").write_bytes(b"%PDF-1.7 fake")
    reseal(root, 13)
    f = _failed(root)
    assert f.code == "V-MANIFEST-INCOMPLETE" and "evidence.pdf" in f.detail


def test_evidence_signature_checked(tmp_path):
    root = copy("pass", tmp_path / "a")
    edit_json(root / "rebases/13/evidence/approvals.json", lambda d: d.update(status_text="approved by everyone"))
    m = root / "rebases/13/evidence/MANIFEST.sha256"
    from test_asset_support import manifest_text

    m.write_text(manifest_text(root, ["rebases/13/MANIFEST.sha256", "rebases/13/evidence/approvals.json"]))
    from roadbed.asset import build_lock, write_lock

    write_lock(root, build_lock(root, previous=json.loads((root / "roadbed.lock").read_text()), synthetic=True))
    f = _failed(root)
    assert f.code == "V-SIG-INVALID" and "MANIFEST.sig" in f.detail


def test_evidence_signature_missing(tmp_path):
    root = copy("pass", tmp_path / "a")
    (root / "rebases/13/evidence/MANIFEST.sig").unlink()
    from roadbed.asset import build_lock, write_lock

    write_lock(root, build_lock(root, previous=json.loads((root / "roadbed.lock").read_text()), synthetic=True))
    assert _failed(root).code == "V-SIG-MISSING"


def test_untrusted_key(tmp_path):
    assert _failed(EXAMPLES / "pass", trusted_keyids={"0" * 64}).code == "V-SIG-UNTRUSTED"


def test_receipt_chain_broken(tmp_path):
    root = copy("pass", tmp_path / "a")
    edit_json(root / "versions/v12/receipt.json", lambda d: d["predicate"].update(ended_at="2026-10-20T08:00:09Z"))
    reseal(root, 13)  # re-signed but not re-chained
    f = _failed(root)
    assert f.code == "V-RECEIPT-CHAIN" and "rebases/13/receipt.json" in f.detail


def test_receipt_must_name_the_predictions(tmp_path):
    root = copy("pass", tmp_path / "a")
    edit_json(root / "rebases/13/receipt.json", lambda d: d.update(subject=d["subject"][:1]))
    reseal(root, 13, chain=True)
    f = _failed(root)
    assert f.code == "V-RECEIPT-MISMATCH" and "predictions" in f.detail


def test_strip_receipt_stays_full(tmp_path):
    """Delete the receipt, its signature, the evidence, the lock and the keys: git still remembers the receipt."""
    root = clone("pass", tmp_path / "pass")
    for rel in ("rebases/13/receipt.json", "rebases/13/receipt.sig", "roadbed.lock"):
        (root / rel).unlink()
    shutil.rmtree(root / "rebases/13/evidence")
    shutil.rmtree(root / "policy/keys")
    shutil.rmtree(root / "versions")
    for seal in root.glob("data/holdout/*/SEAL.json"):
        edit_json(seal, lambda d: [d.pop(k) for k in ("merkle_root", "minhash_oid", "snapshot", "stratification")])
    m = root / "rebases/13/MANIFEST.sha256"
    from test_asset_support import manifest_text

    keep = [ln.split("  ", 1)[1] for ln in m.read_text().splitlines() if "receipt." not in ln]
    m.write_text(manifest_text(root, keep))
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "strip")
    f = _failed(root)
    assert f.code == "V-HASH-MISSING" and "receipt.json" in f.detail


# ---------------------------------------------------------------- synthetic flag, personal data, SoD, contamination


def test_synthetic_flag_lost(tmp_path):
    root = copy("pass", tmp_path / "a")
    edit_json(root / "rebases/13/cost.json", lambda d: d.update(synthetic=False))
    reseal(root, 13)
    f = _failed(root)
    assert f.code == "V-SYNTHETIC-FLAG" and "cost.json" in f.detail


def test_synthetic_flag_dropped_at_root(tmp_path):
    root = copy("pass", tmp_path / "a")
    y = root / "roadbed.yaml"
    y.write_text(y.read_text().replace("synthetic: true\n", "synthetic: false\n", 1))
    reseal(root, 13)
    assert _failed(root).code == "V-SYNTHETIC-FLAG"


def test_synthetic_md_removed(tmp_path):
    root = copy("pass", tmp_path / "a")
    (root / "lineage/SYNTHETIC.md").unlink()
    reseal(root, 13)
    f = _failed(root)
    assert f.code == "V-SYNTHETIC-FLAG" and "lineage/edges.jsonl" in f.detail


def test_email_in_rebase_note(tmp_path):
    root = copy("pass", tmp_path / "a")
    meta = root / "rebases/13/rebase.yaml"
    meta.write_text(meta.read_text().replace("no training in this run", "ask jane.roe@acme-mutual.example"))
    reseal(root, 13)
    assert _failed(root).code == "V-PII"


def test_raw_input_in_holdout_row(tmp_path):
    """An extra raw-text field in a sealed holdout row passes the reader schema but not --strict."""
    root = copy("pass", tmp_path / "a")
    f = root / f"{HOLD}/holdout.jsonl"
    rows = read_doc(f)
    rows[0]["input"] = "Jane Roe rear collision on Elm St"
    f.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
    from roadbed.asset import sha256_file

    edit_json(root / f"{HOLD}/SEAL.json", lambda d: d.update(sha256=sha256_file(f)))
    reseal(root, 13)
    f_ = _failed(root, strict=True)
    assert f_.code == "V-SCHEMA-INVALID" and "holdout.jsonl line 1" in f_.detail


def test_sod01_sealer_requests(tmp_path):
    root = copy("pass", tmp_path / "a")
    meta = root / "rebases/13/rebase.yaml"
    meta.write_text(meta.read_text().replace("requested_by: dana", "requested_by: vik"))
    reseal(root, 13)
    f = _failed(root)
    assert f.code == "V-SOD" and "SoD-01" in f.detail


def test_sod02_requester_approves(tmp_path):
    root = copy("pass", tmp_path / "a")

    def approve(d):
        entry = {"role": "approver", "decision": "approved", "reason_code": "no_findings", "refs": ["rebase:13"],
                 "at": "2026-11-01T12:00:00Z", "audit_seq": 1500, "pack_version": 2}
        d["approvals"]["dana"] = entry
        d["history"].append({"rebase": 13, "actor": "dana", **entry})

    edit_json(root / "rebases/13/evidence/approvals.json", approve)
    reseal(root, 13)
    f = _failed(root)
    assert f.code == "V-SOD" and "SoD-02" in f.detail


def test_contamination_report_must_match_git(tmp_path, store):
    root = clone("pass", tmp_path / "pass")
    edit_json(root / "rebases/13/contamination.json", lambda d: d["seal_order"].update(train_commit="b" * 40))
    reseal(root, 13, commit="edit contamination")
    f = _failed(root, objects=store)
    assert f.code == "V-CONTAMINATION-MISMATCH" and "train_commit" in f.detail


def test_gate_contamination_check_must_match(tmp_path):
    root = copy("pass", tmp_path / "a")

    def drop(d):
        d["checks"] = [c for c in d["checks"] if c["name"] != "contamination"]

    edit_json(root / "rebases/13/gate.json", drop)
    reseal(root, 13)
    assert _failed(root).code == "V-CONTAMINATION-MISMATCH"


def test_rebase_number_must_match(tmp_path):
    root = copy("pass", tmp_path / "a")
    shutil.copytree(root / "rebases/13", root / "rebases/15")
    f = _failed(root, n=15)
    assert f.code in ("V-MANIFEST-INCOMPLETE", "V-LOCK-INCOMPLETE", "V-RECEIPT-MISMATCH", "V-SCORES-MISMATCH")


# ---------------------------------------------------------------- verifier round 2: policy anchor, MinHash, objects

GATE = "policy/gate.yaml"


def _set_policy(root: Path, **kw) -> None:
    text = (root / GATE).read_text()
    for k, v in kw.items():
        text, n = re.subn(rf"^(\s+{k}:) .*$", rf"\g<1> {v}", text, flags=re.M)
        assert n == 1, k
    (root / GATE).write_text(text)


def _near_dup_asset(tmp_path: Path, *, holdout_raw: bool):
    """Training gets holdout row 7 with its last word changed: Jaccard 21/23 = 0.913 over 3-shingles."""
    b = builder()
    store = bucket(tmp_path / "b", holdout_raw=holdout_raw)
    words = b.narrative("h", 7).split()
    words[-1] = "zebra"
    oid = store.put(" ".join(words).encode())
    root = copy("pass", tmp_path / "a")
    row = {**_holdout_source_row(7), "input_ref": f"sha256:{oid}", "ts": "2026-10-30T12:00:00Z", "interaction_id": "int-t-9998"}
    _fix_snapshot(root, read_doc(root / f"{SNAP}/part-0000.jsonl") + [row])
    return root, store


@pytest.mark.parametrize("policy", [{"minhash_threshold": 0.95}, {"shingle": 30}, {"num_perm": 64}],
                         ids=["threshold", "shingle", "num_perm"])
def test_policy_tamper_hides_near_duplicate(tmp_path, policy):
    """Raising the contamination threshold (or shingle) in policy/gate.yaml hides a 0.913 near duplicate.
    gate.json policy_sha256 anchors the policy the gate ran with."""
    root, store = _near_dup_asset(tmp_path, holdout_raw=True)
    reseal(root, 13, chain=True)
    f = _failed(root, objects=store.root)
    assert f.code == "V-HOLDOUT-OVERLAP"  # baseline: caught at 0.8
    _set_policy(root, **policy)
    reseal(root, 13, chain=True)  # relock only; gate.json and contamination.json untouched
    f = _failed(root, objects=store.root)
    assert f.code == "V-POLICY-MISMATCH" and "policy_sha256" in f.detail


def test_policy_tamper_in_git_clone_strict_pinned(tmp_path):
    """Only gate.yaml and roadbed.lock change, committed; --strict, bundle clone, pinned keys."""
    root = clone("pass", tmp_path / "a")
    store = bucket(tmp_path / "b")
    _set_policy(root, minhash_threshold=0.95)
    lock = root / "roadbed.lock"
    write_lock(root, build_lock(root, store=store, previous=json.loads(lock.read_text()), synthetic=True))
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "raise contamination threshold")
    f = _failed(root, objects=store.root, strict=True, trusted_keyids=KEYS)
    assert f.code == "V-POLICY-MISMATCH"


def test_policy_tamper_with_gate_hash_rewritten(tmp_path):
    """The attacker also rewrites gate.json policy_sha256: contamination.json still says threshold 0.8."""
    root, store = _near_dup_asset(tmp_path, holdout_raw=True)
    _set_policy(root, minhash_threshold=0.95)
    policy = GatePolicy.model_validate(yaml.safe_load((root / GATE).read_text()))
    edit_json(root / "rebases/13/gate.json",
              lambda d: d.update(policy_sha256=jcs_sha256(policy.model_dump(mode="json", exclude_none=True))))
    reseal(root, 13, chain=True)
    f = _failed(root, objects=store.root)
    assert f.code == "V-CONTAMINATION-MISMATCH" and "near.threshold" in f.detail


@pytest.mark.parametrize("field,value", [("threshold", 0.95), ("num_perm", 64)])
def test_contamination_near_parameters_must_match_policy(tmp_path, field, value):
    root = copy("pass", tmp_path / "a")
    edit_json(root / "rebases/13/contamination.json", lambda d: d["near"].update({field: value}))
    reseal(root, 13)
    f = _failed(root)
    assert f.code == "V-CONTAMINATION-MISMATCH" and f"near.{field}" in f.detail


def test_policy_hash_known_answer():
    """Both fixtures: sha256(JCS(policy)) of policy/gate.yaml is the gate.json policy_sha256."""
    for asset, n in REBASE.items():
        r = verify(EXAMPLES / asset / f"rebases/{n}")
        pol = next(c for c in r.checks if c.code == "V-POLICY-MISMATCH")
        assert pol.ok is True and pol.detail.startswith("db986e9ba495")


def test_minhash_object_signatures_must_match_raw_inputs(tmp_path):
    """A seal whose MinHash object has the right row hashes but garbage signatures: with the raw holdout inputs in
    --objects, verify recomputes the signatures."""
    store = bucket(tmp_path / "b", holdout_raw=True)
    root = copy("pass", tmp_path / "a")
    seal = read_doc(root / f"{HOLD}/SEAL.json")
    n, k, sigs = parse_minhash_object(store.get(seal["minhash_oid"]))
    forged = store.put(minhash_object({h: ["0" * 16] * n for h in sigs}, n, k))
    edit_json(root / f"{HOLD}/SEAL.json", lambda d: d.update(minhash_oid=forged))
    reseal(root, 13, store=store)
    f = _failed(root, objects=store.root)
    assert f.code == "V-HASH-MISMATCH" and "MinHash object" in f.detail


def test_named_object_missing_from_objects(tmp_path, store):
    """The receipt names an adapter (oid: subject) the bucket copy lacks: a skipped check, a failure with --strict."""
    root = clone("pass", tmp_path / "a")
    rec = read_doc(root / "rebases/13/receipt.json")
    adapter = next(s["name"] for s in rec["subject"] if s["name"].startswith("oid:")).removeprefix("oid:")
    r = verify(root / "rebases/13", objects=store)
    assert r.ok
    last = r.checks[-1]
    assert last.code == "V-OBJECT-MISSING" and last.ok is None and adapter[:12] in last.detail
    f = _failed(root, objects=store, strict=True, trusted_keyids=KEYS)
    assert f.code == "V-OBJECT-MISSING" and "--strict" in f.detail


# ---------------------------------------------------------------- receipt config binding

CONFIG_FILES = {"roadbed.yaml": "roadbed_yaml_sha256", "evals/slices.yaml": "slices_sha256",
                "evals/scorers.yaml": "scorers_sha256", GATE: "gate_policy_sha256", f"{HOLD}/SEAL.json": "seal_sha256"}


def _config_check(r):
    return next(c for c in r.checks if c.label.startswith("receipt binds its config"))


@pytest.mark.parametrize("asset", ["pass", "blocked"])
def test_config_binds_files_and_start_lock_in_git(asset, tmp_path, store):
    """Bundle clone: the five files match the working tree and the commit that added the receipt, and lock_sha256
    is the lock in that commit (not the later one in the working tree)."""
    root = clone(asset, tmp_path / asset)
    r = verify(root / f"rebases/{REBASE[asset]}", objects=store)
    assert r.ok, r.failed
    c = _config_check(r)
    assert c.ok is True and c.detail.startswith("5 files and roadbed.lock from the commit that added")


@pytest.mark.parametrize("rel", list(CONFIG_FILES))
def test_config_file_tampered(tmp_path, rel):
    """The attacker changes one config file and relocks (manifests and lock consistent), but cannot re-sign a
    receipt whose config still names the old bytes."""
    root = copy("pass", tmp_path / "a")
    f = root / rel
    if rel.endswith(".json"):
        f.write_text(json.dumps(json.loads(f.read_text()), indent=4) + "\n")  # same values, other bytes
    else:
        f.write_text(f.read_text() + "# tampered\n")
    reseal(root, 13, rebind=False)
    f_ = _failed(root)
    assert f_.code == "V-HASH-MISMATCH" and rel in f_.detail and CONFIG_FILES[rel] in f_.detail


def test_config_margin_relock_refused(tmp_path):
    """evals.margin loosened in roadbed.yaml plus a relock: refused by config.roadbed_yaml_sha256."""
    root = copy("pass", tmp_path / "a")
    y = root / "roadbed.yaml"
    y.write_text(y.read_text().replace("margin: -2.0", "margin: -50.0"))
    reseal(root, 13, rebind=False)
    f = _failed(root)
    assert f.code == "V-HASH-MISMATCH" and "roadbed.yaml" in f.detail


def test_config_committed_tamper_refused(tmp_path, store):
    """A committed edit of a config file after the receipt: the working tree no longer matches the receipt."""
    root = clone("pass", tmp_path / "a")
    s = root / "evals/scorers.yaml"
    s.write_text(s.read_text() + "# tampered\n")
    reseal(root, 13, sign=False, rebind=False, commit="tune scorers")
    f = _failed(root, objects=store)
    assert f.code == "V-HASH-MISMATCH" and "evals/scorers.yaml" in f.detail


def test_config_start_lock_tampered_in_history(tmp_path, store):
    """Rewrite the commit that added the receipt with a different roadbed.lock; the tip tree is unchanged."""
    root = clone("pass", tmp_path / "a")
    tip = git(root, "rev-parse", "HEAD")
    train = git(root, "log", "--diff-filter=A", "--format=%H", "--", "rebases/13/receipt.json")
    git(root, "reset", "-q", "--hard", train)
    lock = root / "roadbed.lock"
    lock.write_text(lock.read_text() + "\n")
    git(root, "commit", "-q", "-a", "--amend", "--no-edit")
    git(root, "checkout", "-q", tip, "--", ".")
    git(root, "commit", "-q", "-m", "outputs")
    f = _failed(root, objects=store)
    assert f.code == "V-HASH-MISMATCH" and "roadbed.lock" in f.detail and "lock_sha256" in f.detail


def test_receipt_without_config_still_verifies(tmp_path):
    """Compatibility: config is optional."""
    root = copy("pass", tmp_path / "a")
    edit_json(root / "rebases/13/receipt.json", lambda d: d["predicate"].__delitem__("config"))
    reseal(root, 13)
    r = verify(root / "rebases/13")
    assert r.ok, r.failed
    c = _config_check(r)
    assert c.ok is True and "no config" in c.detail


def test_config_start_lock_without_history(tmp_path):
    """No git history, and the working tree lock does not list the receipt: it is still the start lock and is
    checked. A wrong lock_sha256 then fails."""
    root = copy("pass", tmp_path / "a")
    edit_json(root / "rebases/13/receipt.json", lambda d: d["predicate"]["config"].update(lock_sha256="0" * 64))
    reseal(root, 13)
    lock = root / "roadbed.lock"
    doc = json.loads(lock.read_text())
    doc["files"] = [e for e in doc["files"] if not e["path"].startswith("rebases/13/")]
    lock.write_text(json.dumps(doc, indent=2) + "\n")
    c = _config(_Ctx(open_asset(root), root / "rebases/13", 13, None, False, None))
    assert c.ok is False and c.code == "V-HASH-MISMATCH" and "lock_sha256" in c.detail


def test_a_gate_path_outside_the_asset_fails_and_its_file_is_never_echoed(tmp_path):
    root = copy("pass", tmp_path / "a")
    secret = tmp_path / "outside_secret.yaml"
    secret.write_text('margin: -2.0\nsecret_api_key: "sk-live-THIS-IS-A-SECRET-OUTSIDE-ASSET"\n')
    cfg = yaml.safe_load((root / "roadbed.yaml").read_text())
    cfg["gate"] = str(secret)
    (root / "roadbed.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    f_ = _failed(root)
    assert f_.code == "V-SCHEMA-INVALID", f_.detail
    assert "sk-live" not in f_.detail and "margin" not in f_.detail
