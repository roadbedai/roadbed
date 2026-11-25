"""Eval synthesis on the 2k demo fixture: disjointness, stratification, correction tests, determinism, schemas."""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections import Counter

import jsonschema
import pytest
from test_evals_support import AS_OF, SEED, events, fresh, output_schema, published_schema, slices

from conftest import make_policy
from roadbed.asset import ObjectStore, jaccard, merkle_root, shingles, validate_file
from roadbed.asset.hashing import jcs_sha256
from roadbed.contamination import check as contamination_check
from roadbed.contracts import ContaminationPolicy, DatasetRef, SealOrder
from roadbed.evals import (
    EvalPolicy,
    LeakageError,
    assert_disjoint,
    build,
    read_candidate,
    read_seen,
    read_snapshots,
    seal_candidate,
    write_candidate,
)
from roadbed.labels import resolve
from roadbed.labels.rules import weight

KEYS = ("line", "state", "channel")


@pytest.fixture(scope="module")
def resolved():
    return resolve(events("interactions"), events("verdicts"), events("outcomes"), as_of=AS_OF, output_schema=output_schema())


@pytest.fixture(scope="module")
def out(resolved):
    return build(resolved, slices(), EvalPolicy(), SEED, as_of=AS_OF, output_schema=output_schema())


def part_rows(out) -> list[dict]:
    return [json.loads(x) for p in out.snapshot.parts for x in p.data.decode().splitlines()]


# ---------------------------------------------------------------- disjointness


def test_train_and_holdout_share_nothing(out, resolved):
    train, hold = out.snapshot.rows, out.holdout_candidate.rows
    assert len(hold) > 250 and len(train) > 1500
    assert not {r["interaction_id"] for r in train} & {r["id"] for r in hold}
    assert not {r["row_hash"] for r in train} & {r["row_hash"] for r in hold}
    assert not {r["input_ref"] for r in train} & {r["input_ref"] for r in hold}
    # near overlap, recomputed with the asset shingles and Jaccard over every pair (not the build's own index)
    text = {r["input_ref"]: r["input_text"] for r in resolved if r["use"] != "quarantine"}
    hs = [shingles(text[r["input_ref"]]) for r in hold]
    worst = max(jaccard(shingles(text[r["input_ref"]]), h) for r in train[::7] for h in hs)
    assert worst < EvalPolicy().purge_threshold
    assert out.proof["pairs"]["train-holdout"]["near_pairs"] == 0


def test_a_holdout_row_never_appears_in_the_snapshot_parts(out, tmp_path):
    hold = out.holdout_candidate.rows
    ids, hashes, refs = {r["id"] for r in hold}, {r["row_hash"] for r in hold}, {r["input_ref"] for r in hold}
    rows = part_rows(out)
    assert len(rows) == out.snapshot.manifest["rows"]
    assert not any(r["interaction_id"] in ids or r["row_hash"] in hashes or r["input_ref"] in refs for r in rows)
    out.write(tmp_path)
    blob = b"".join(p.read_bytes() for p in (tmp_path / "data" / "snapshots").rglob("*") if p.is_file())
    blob += b"".join((tmp_path / "evals" / n).read_bytes() for n in ("golden.jsonl", "correction_tests.jsonl", "seen.jsonl"))
    for r in hold:  # no id, row hash or input address of a holdout row in any file written to the asset
        assert r["id"].encode() not in blob and r["row_hash"].encode() not in blob and r["input_ref"].encode() not in blob


def test_golden_and_eval_only_are_disjoint_from_holdout_and_train(out):
    hold = {r["id"] for r in out.holdout_candidate.rows}
    gold = {r["id"] for r in out.golden}
    train = {r["interaction_id"] for r in out.snapshot.rows}
    ev = {r["interaction_id"] for r in out.eval_only}
    assert not hold & gold and not train & gold and not ev & (hold | gold | train)
    assert len(ev) == 26  # every escalation, none in training


def test_the_w03_contamination_check_passes_on_the_sealed_candidate(out, resolved, tmp_path):
    """Seal the candidate through the asset library and run the gate's contamination check (MinHash, as in a rebase)."""
    store = ObjectStore(tmp_path / "bucket")
    seal = seal_candidate(out.holdout_candidate, tmp_path / "asset", "H-2026-11-01-s7", sealed_by="vik",
                          sealed_at="2026-11-01T09:02:00Z", audit_seq=1204, store=store)
    assert seal["merkle_root"] == out.holdout_candidate.merkle_root and seal["snapshot"] == out.snapshot.id
    assert store.has(seal["minhash_oid"])
    texts = {r["input_ref"]: r["input_text"] for r in resolved if r["use"] != "quarantine"}
    policy = make_policy(contamination=ContaminationPolicy(minhash_threshold=0.8, num_perm=128, shingle=3))
    rep = contamination_check(
        out.snapshot.rows, out.holdout_candidate.rows, policy, texts=texts, rebase=13,
        holdout=DatasetRef(id=seal["id"], merkle_root=seal["merkle_root"], rows=seal["rows"]),
        train=DatasetRef(id=out.snapshot.id, merkle_root=out.snapshot.manifest["merkle_root"], rows=out.snapshot.manifest["rows"]),
        seal_order=SealOrder(seal_commit="a" * 40, train_commit="b" * 40, audit_seq=True, timestamp=True, git_ancestor=True, ok=True),
        synthetic=True,
    )
    assert rep.status == "pass" and rep.exact.shared == 0 and not rep.near.pairs


def test_assert_disjoint_catches_a_shared_row_and_a_near_copy(out, resolved):
    texts = {r["input_ref"]: r["input_text"] for r in resolved if r["use"] != "quarantine"}
    train, hold = out.snapshot.rows, out.holdout_candidate.rows
    assert assert_disjoint(train, hold, texts)["near_pairs"] == 0
    with pytest.raises(LeakageError, match="shared_row_hashes"):
        assert_disjoint(train + [{**hold[0], "interaction_id": "copy"}], hold, texts)
    # a near copy: the same input with one word changed, under a new input address
    src = json.loads(texts[hold[0]["input_ref"]])
    src["narrative"] = src["narrative"].replace(" ", "  ", 1) + " ok"
    t2 = json.dumps(src, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    ref2 = "sha256:" + hashlib.sha256(t2.encode()).hexdigest()
    near = {**train[0], "interaction_id": "near", "input_ref": ref2, "row_hash": "f" * 64}
    with pytest.raises(LeakageError, match="near_pairs"):
        assert_disjoint(train + [near], hold, {**texts, ref2: t2})


# ---------------------------------------------------------------- stratification


def test_holdout_is_stratified_within_two_points(out, resolved):
    pool = [r for r in resolved if r["use"] == "train"]
    hold = out.holdout_candidate.rows
    assert len(hold) == round(0.15 * len(pool))  # no shared inputs in this fixture, so every labelled row is eligible
    for k in KEYS:
        cp, ch = Counter(r["slices"][k] for r in pool), Counter(r["slices"][k] for r in hold)
        for v in cp:
            assert abs(ch[v] / len(hold) - cp[v] / len(pool)) <= 0.02, (k, v)
    assert out.report["stratification"]["holdout_max_share_gap"] <= 0.02
    assert out.holdout_candidate.stratification == {"keys": list(KEYS), "method": "proportional", "fraction": 0.15}


def test_golden_is_stratified_within_two_points(out, resolved):
    pool = [r for r in resolved if r["use"] == "train"]
    for k in KEYS:
        cp, cg = Counter(r["slices"][k] for r in pool), Counter(r["slices"][k] for r in out.golden)
        for v in cp:
            assert abs(cg[v] / len(out.golden) - cp[v] / len(pool)) <= 0.02, (k, v)


# ---------------------------------------------------------------- correction tests


def test_exactly_one_correction_test_per_edit(out, resolved):
    fixes = {r["interaction_id"]: r for r in resolved if r["use"] == "train" and r["action"] == "fix"}
    cts = out.correction_tests
    hold = {r["id"] for r in out.holdout_candidate.rows}
    assert len({c["interaction_id"] for c in cts}) == len(cts)
    assert {c["interaction_id"] for c in cts} == set(fixes) - hold
    e = out.report["edits"]
    assert e["total"] == len(fixes) == 236
    assert e["correction_tests"] + e["withheld_holdout"] + e["withheld_purged"] + e["withheld_sealed"] == e["total"]
    assert e["withheld_holdout"] == len(set(fixes) & hold)
    for c in cts:
        f = fixes[c["interaction_id"]]
        assert c["expected"] == f["fix"] and c["model_output"] == f["output"] and c["reason_code"] == f["reason_code"]
        assert c["model_version"] == "v12" and c["created_at"] == f["ts"] and c["row_hash"] == f["row_hash"]
    assert [c["id"] for c in cts] == [f"CT-{i:04d}" for i in range(1, len(cts) + 1)]


def test_edits_weigh_two_in_the_snapshot(out):
    w = Counter(weight(r) for r in out.snapshot.rows)
    fixes = sum(1 for r in out.snapshot.rows if r["reason_code"] is not None)
    assert w[2.0] == fixes and w[1.0] == len(out.snapshot.rows) - fixes


# ---------------------------------------------------------------- determinism and speed


def _bytes(o) -> dict:
    return {
        "parts": [p.data for p in o.snapshot.parts], "manifest": json.dumps(o.snapshot.manifest, sort_keys=True),
        "holdout": json.dumps(o.holdout_candidate.rows), "golden": json.dumps(o.golden), "cts": json.dumps(o.correction_tests),
        "report": json.dumps(o.report, sort_keys=True), "proof": json.dumps(o.proof, sort_keys=True),
    }


def test_deterministic_for_a_seed(out, resolved):
    again = build(resolved, slices(), EvalPolicy(), SEED, as_of=AS_OF, output_schema=output_schema())
    assert _bytes(again) == _bytes(out)
    shuffled = list(resolved)
    random.Random(3).shuffle(shuffled)
    assert _bytes(build(shuffled, slices(), EvalPolicy(), SEED, as_of=AS_OF, output_schema=output_schema())) == _bytes(out)
    other = build(resolved, slices(), EvalPolicy(), SEED + 1, as_of=AS_OF, output_schema=output_schema())
    assert {r["id"] for r in other.holdout_candidate.rows} != {r["id"] for r in out.holdout_candidate.rows}


def test_resolve_and_build_under_ten_seconds_on_cpu():
    t0 = time.perf_counter()
    rows = resolve(events("interactions"), events("verdicts"), events("outcomes"), as_of=AS_OF, output_schema=output_schema())
    build(rows, slices(), EvalPolicy(), SEED, as_of=AS_OF, output_schema=output_schema())
    assert time.perf_counter() - t0 < 10.0


# ---------------------------------------------------------------- schemas and the manifest


def _check(rows, name):
    v = jsonschema.Draft202012Validator(published_schema(name))
    for r in rows:
        v.validate(r)


def test_outputs_validate_against_the_published_schemas(out, tmp_path):
    _check([out.snapshot.manifest], "snapshot-manifest")
    _check(part_rows(out), "snapshot-row")
    _check(out.holdout_candidate.rows, "holdout-row")
    _check(out.golden, "golden-row")
    _check(out.correction_tests, "correction-test-row")
    written = out.write(tmp_path)
    for rel in written:  # and through the asset path rules, closed form
        validate_file(tmp_path, rel, strict=True)


def test_manifest_binds_parts_rows_and_rules(out):
    m = out.snapshot.manifest
    assert m["id"] == "S-2026-11-01" and m["label_rules"] == "label_rules@v1" and m["synthetic"] is True
    assert m["pseudonymisation"] == {"profile": "insurer-default@v1", "hmac_key_id": "synthetic-seed-7"}
    assert m["schema_sha256"] == jcs_sha256(output_schema())
    assert m["merkle_root"] == merkle_root(r["row_hash"] for r in part_rows(out))
    for p, mp in zip(out.snapshot.parts, m["parts"]):
        assert mp["oid"] == hashlib.sha256(p.data).hexdigest() and mp["size"] == len(p.data) <= 1_000_000
    assert sum(p["rows"] for p in m["parts"]) == m["rows"]


def test_small_part_limit_splits_and_keeps_the_root(resolved, out):
    small = build(resolved, slices(), EvalPolicy(part_bytes=100_000), SEED, as_of=AS_OF, output_schema=output_schema())
    assert len(small.snapshot.parts) > 5
    assert small.snapshot.manifest["merkle_root"] == out.snapshot.manifest["merkle_root"]
    assert b"".join(p.data for p in small.snapshot.parts) == b"".join(p.data for p in out.snapshot.parts)


# ---------------------------------------------------------------- files


def test_write_marks_synthetic_and_refuses_to_overwrite(out, tmp_path):
    store = ObjectStore(tmp_path / "bucket")
    written = out.write(tmp_path / "a", store=store)
    assert "data/snapshots/2026-11-01/MANIFEST.json" in written and "evals/SYNTHETIC.md" in written
    assert (tmp_path / "a" / "data" / "snapshots" / "2026-11-01" / "SYNTHETIC.md").is_file()
    assert all(store.has(p.oid) for p in out.snapshot.parts)
    report = json.loads((tmp_path / "a" / "data" / "snapshots" / "2026-11-01" / "build.json").read_text())
    assert report["report"]["counts"]["holdout_candidate"] == len(out.holdout_candidate)
    with pytest.raises(FileExistsError, match="frozen"):
        out.write(tmp_path / "a")


def test_candidate_goes_to_the_validator_only(out, tmp_path):
    asset = tmp_path / "asset"
    asset.mkdir()
    with pytest.raises(ValueError, match="never into the asset"):
        write_candidate(out.holdout_candidate, asset / "data" / "holdout" / "x", asset_root=asset)
    paths = write_candidate(out.holdout_candidate, tmp_path / "validator", asset_root=asset)
    assert all(p.stat().st_mode & 0o077 == 0 for p in paths)  # owner only
    back = read_candidate(tmp_path / "validator")
    assert back.rows == out.holdout_candidate.rows and back.inputs == out.holdout_candidate.inputs
    assert "rows=" not in repr(out.holdout_candidate) and "inputs=" not in repr(out.holdout_candidate)
    with pytest.raises(FileExistsError):
        write_candidate(out.holdout_candidate, tmp_path / "validator")


def test_a_sealed_holdout_stays_out_of_the_next_build(out, resolved):
    nxt = build(resolved, slices(), EvalPolicy(), SEED + 5, as_of=AS_OF, output_schema=output_schema(),
                sealed_holdout=out.holdout_candidate.rows, suffix="b")
    sealed = {r["id"] for r in out.holdout_candidate.rows}
    assert nxt.report["counts"]["excluded_by_seal"] == len(sealed)
    seen = {r["interaction_id"] for r in nxt.snapshot.rows} | {r["id"] for r in nxt.golden} | {r["id"] for r in nxt.holdout_candidate.rows}
    assert not seen & sealed
    assert nxt.snapshot.id == "S-2026-11-01-b"
    assert nxt.report["edits"]["withheld_sealed"] == sum(1 for r in resolved if r["interaction_id"] in sealed and r["action"] == "fix")


def test_mixed_synthetic_and_real_rows_are_refused(resolved):
    rows = [dict(r) for r in resolved]
    rows[5]["synthetic"] = False
    with pytest.raises(ValueError, match="synthetic"):
        build(rows, slices(), EvalPolicy(), SEED, as_of=AS_OF, output_schema=output_schema())


def test_undeclared_slice_values_are_refused(resolved):
    rows = [dict(r) for r in resolved]
    rows[0] = {**rows[0], "slices": {**rows[0]["slices"], "state": "ZZ"}}
    with pytest.raises(ValueError, match="not declared"):
        build(rows, slices(), EvalPolicy(), SEED, as_of=AS_OF, output_schema=output_schema())


def test_tampered_input_text_is_refused(resolved):
    rows = [dict(r) for r in resolved]
    rows[0] = {**rows[0], "input_text": rows[0]["input_text"].replace("a", "b", 1)}
    with pytest.raises(ValueError, match="input_ref"):
        build(rows, slices(), EvalPolicy(), SEED, as_of=AS_OF, output_schema=output_schema())


def test_the_third_build_in_an_asset_counts_each_trained_row_once(out, tmp_path):
    """Snapshots repeat rows, and two seals can share rows. Repeats must not double shingle counts (a false near
    pair, Jaccard above 1) or inflate the proof counts. Each later build gets new labelled rows: with everything
    pinned (sealed, trained on, seen), the same rows leave nothing to hold out."""
    sealed = out.holdout_candidate.rows
    out.write(tmp_path)

    def more(n):
        ints, vds = fresh(n, "x")
        return resolve(events("interactions") + ints, events("verdicts") + vds, events("outcomes"), as_of=AS_OF, output_schema=output_schema())

    second = build(more(300), slices(), EvalPolicy(), SEED, as_of=AS_OF, output_schema=output_schema(),
                   sealed_holdout=sealed, trained_on=read_snapshots(tmp_path), seen=read_seen(tmp_path), suffix="b")
    second.write(tmp_path)
    prior = read_snapshots(tmp_path)
    unique = {r["row_hash"] for r in prior}
    assert len(prior) > len(unique), "the probe needs rows repeated across snapshots"
    third = build(more(600), slices(), EvalPolicy(), SEED, as_of=AS_OF, output_schema=output_schema(),
                  sealed_holdout=sealed + sealed, trained_on=prior, seen=read_seen(tmp_path), suffix="c")
    pairs = third.proof["pairs"]
    for name in ("holdout-trained_on", "golden-trained_on"):
        assert pairs[name]["rows"][1] == len(unique)
        assert pairs[name]["near_pairs"] == 0
    for name in ("train-sealed", "golden-sealed"):
        assert pairs[name]["rows"][1] == len(sealed)
    assert pairs["holdout-seen"]["rows"][1] == len(read_seen(tmp_path))
    assert all(p["max_jaccard"] <= 1 for p in pairs.values())


def test_training_inputs_are_kept_out_of_repr(out):
    train = out.snapshot.rows
    assert set(out.train_inputs) == {r["input_ref"] for r in train}
    assert not set(out.train_inputs) & {r["input_ref"] for r in out.holdout_candidate.rows}
    assert "train_inputs=" not in repr(out) and next(iter(out.train_inputs.values())) not in repr(out)
