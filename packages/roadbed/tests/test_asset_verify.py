"""W02 verify: passes on both fixture assets, and each tamper fails with its V-* code.

Most tampers go through reseal(): the attacker holds the signing keys and makes every hash, manifest, signature and
the lock consistent again, so only the check under test can catch the change.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from roadbed.asset import read_doc, row_hash, verify
from roadbed.cli.main import app
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
    """In the spec repo the fixtures have no git history of their own and no bucket: those checks are skipped."""
    r = verify(EXAMPLES / asset / f"rebases/{REBASE[asset]}")
    assert r.ok and r.full
    skipped = sorted(c.label for c in r.checks if c.ok is None)
    assert skipped == ["seal precedes training: git ancestry: skipped", "train and holdout disjoint: near (Jaccard < 0.8): skipped"]


@pytest.mark.parametrize("asset", ["pass", "blocked"])
def test_passes_strict_on_bundle_clone(asset, tmp_path, store):
    """The real history and the bucket: every check runs (none skipped), closed schemas, pinned keys."""
    root = clone(asset, tmp_path / asset)
    r = verify(root / f"rebases/{REBASE[asset]}", objects=store, strict=True, trusted_keyids=KEYS)
    assert r.ok, r.failed
    assert all(c.ok is True for c in r.checks)
    codes = {c.code for c in r.checks}
    assert {"V-SEAL-ORDER", "V-HOLDOUT-OVERLAP", "V-SIG-INVALID", "V-RECEIPT-CHAIN", "V-LOCK-INCOMPLETE"} <= codes
    near = next(c for c in r.checks if c.label.startswith("train and holdout disjoint: near"))
    assert near.detail == "jaccard, max 0.02381"  # known answer: contamination.json max_jaccard


def test_minhash_fallback(tmp_path):
    """Without the raw holdout inputs, near overlap uses the sealed MinHash object."""
    root = clone("pass", tmp_path / "pass")
    b = bucket(tmp_path / "b", holdout_raw=False)
    r = verify(root / "rebases/13", objects=b.root, strict=True)
    assert r.ok, r.failed
    assert any(c.detail.startswith("minhash, max") for c in r.checks)


def test_strict_fails_on_skips():
    f = _failed(EXAMPLES / "pass", strict=True)
    assert f.code == "V-SEAL-ORDER" and "--strict" in f.detail


def test_cli_exit_4_names_the_check(tmp_path, store):
    root = clone("pass", tmp_path / "pass")
    r = runner.invoke(app, ["verify", "rebases/13", "--asset", str(root), "--strict", "--objects", str(store), "--json"])
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
    from roadbed.asset.overlap import minhash_object, parse_minhash_object

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
