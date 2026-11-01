"""W14 verifier findings (PR #7): one test per finding. Round 1 tests fail on 4279b4f, round 4 tests on af92289."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import shutil

import pytest
from test_evals_cli import ARGS, _asset
from test_evals_support import AS_OF, SEED, events, fresh, input_ref, interaction, outcome, output_schema, slices, verdict
from typer.testing import CliRunner

from roadbed.asset import ObjectStore, jaccard, shingles
from roadbed.cli.evals import read_sealed
from roadbed.cli.main import app
from roadbed.evals import EvalPolicy, LeakageError, build, read_seen, read_snapshots, seal_candidate
from roadbed.labels import resolve

EMAIL = "jane.doe@example.com"


def _resolve(ints, vds, ocs=()):
    return resolve(ints, vds, list(ocs), as_of=AS_OF, output_schema=output_schema())


def _build(rows, **kw):
    return build(rows, slices(), EvalPolicy(), SEED, as_of=AS_OF, output_schema=output_schema(), **kw)


def _everywhere(out) -> int:
    """Rows of every developer-visible output and the candidate that hold EMAIL."""
    blob = b"".join(p.data for p in out.snapshot.parts)
    others = [*out.holdout_candidate.rows, *out.golden, *out.correction_tests, *out.eval_only]
    return blob.count(EMAIL.encode()) + sum(EMAIL in json.dumps(r) for r in others) + (EMAIL in json.dumps(out.report))


# ---------------------------------------------------------------- 1. PII in slices


def test_email_in_an_undeclared_slice_key_is_quarantined_and_reaches_no_output():
    ints = events("interactions")
    for i in ints:
        i["slices"]["claimant_email"] = EMAIL
    rows = _resolve(ints, events("verdicts"), events("outcomes"))
    assert all(r["use"] == "quarantine" for r in rows)
    assert not any(EMAIL in r["reason"] for r in rows)


def test_build_refuses_an_undeclared_slice_key_even_with_a_harmless_value():
    ints = events("interactions")
    ints[0]["slices"]["region"] = "west"
    rows = _resolve(ints, events("verdicts"), events("outcomes"))
    assert rows[0]["use"] != "quarantine"
    with pytest.raises(ValueError, match="not declared in slices.yaml") as e:
        _build(rows)
    assert "west" not in str(e.value)


def test_email_in_one_declared_slice_value_reaches_no_output():
    ints = events("interactions")
    ints[5]["slices"]["state"] = EMAIL
    rows = _resolve(ints, events("verdicts"), events("outcomes"))
    assert [r["use"] for r in rows if r["interaction_id"] == ints[5]["interaction_id"]] == ["quarantine"]
    assert _everywhere(_build(rows)) == 0


# ---------------------------------------------------------------- 4. interaction_id and version


def test_contact_shaped_interaction_id_is_quarantined_under_a_redacted_id():
    it = interaction(EMAIL, narrative="a hail storm dented the roof and broke two skylights last night")
    rows = _resolve([it], [verdict(EMAIL, "approve")])
    assert rows[0]["use"] == "quarantine" and rows[0]["interaction_id"].startswith("redacted:")
    assert EMAIL not in json.dumps(rows)


def test_email_in_version_is_quarantined():
    rows = _resolve([interaction("i1", version=f"v12 deployed by {EMAIL}")], [verdict("i1", "fix")])
    assert rows[0]["use"] == "quarantine" and EMAIL not in rows[0]["reason"]


# ---------------------------------------------------------------- 5. numeric PII


@pytest.mark.parametrize("body", [
    {"final_route": "siu", "phone_number": "x"},
    {"final_route": "siu", "ssn_last": "x"},
    {"final_route": "siu", "code": 14155550134},
    {"final_route": "siu", "code": 123456789},
])
def test_numeric_and_pii_named_outcome_fields_are_quarantined(body):
    rows = _resolve([interaction("i1")], [verdict("i1", "approve")], [outcome("i1", body)])
    assert rows[0]["use"] == "quarantine"


def test_short_numbers_still_pass():
    rows = _resolve([interaction("i1")], [verdict("i1", "approve")], [outcome("i1", {"final_route": "siu", "days": 12345678})])
    assert rows[0]["use"] == "train"


# ---------------------------------------------------------------- 6. lost synthetic flag


def test_syn_ids_without_the_synthetic_flag_are_refused():
    ints = events("interactions")
    for i in ints:
        i.pop("synthetic", None)
    rows = _resolve(ints, events("verdicts"), events("outcomes"))
    with pytest.raises(ValueError, match="syn- ids but synthetic is not true"):
        _build(rows)


# ---------------------------------------------------------------- 3. near copy of a sealed row


def test_near_copy_of_a_sealed_row_is_purged_when_its_text_comes_from_an_exact_twin():
    ints, vds = events("interactions"), events("verdicts")
    sealed = _build(_resolve(ints, vds)).holdout_candidate.rows
    by_id = {i["interaction_id"]: i for i in ints}
    orig = by_id[max(sealed, key=lambda r: len(by_id[r["id"]]["input"]["narrative"].split()))["id"]]
    exact = copy.deepcopy(orig)
    exact["interaction_id"] += "-exact"
    near = copy.deepcopy(orig)
    near["interaction_id"] += "-near"
    near["input"]["narrative"] += " Resubmitted."
    near["input_ref"] = input_ref(near["input"])
    assert jaccard(shingles(json.dumps(orig["input"])), shingles(json.dumps(near["input"]))) > 0.8
    v = {x["interaction_id"]: x for x in vds}[orig["interaction_id"]]
    ints2 = [i for i in ints if i is not orig] + [exact, near]  # the sealed row's own interaction is gone (erased)
    vds2 = [x for x in vds if x is not v] + [dict(v, verdict_id="vd-e", interaction_id=exact["interaction_id"]),
                                             dict(v, verdict_id="vd-n", interaction_id=near["interaction_id"])]
    out = _build(_resolve(ints2, vds2), sealed_holdout=sealed, suffix="b")
    visible = {r["interaction_id"] for r in out.snapshot.rows} | {r["id"] for r in out.golden} | {r["id"] for r in out.holdout_candidate.rows}
    assert exact["interaction_id"] not in visible and near["interaction_id"] not in visible
    p = out.proof["pairs"]["train-sealed"]
    assert p["near_pairs"] == 0 and p["exact_only"] == 0


# ---------------------------------------------------------------- 8. read_snapshots re-derives the Merkle root


def _written(tmp_path):
    root = tmp_path / "asset"
    root.mkdir()
    out = _build(_resolve(events("interactions"), events("verdicts"), events("outcomes")))
    out.write(root)
    return root, out


def test_read_snapshots_fails_closed_when_a_row_and_its_part_oid_are_edited(tmp_path):
    import hashlib

    root, out = _written(tmp_path)
    assert len(read_snapshots(root)) == len(out.snapshot.rows)
    man_p = next((root / "data" / "snapshots").glob("*/MANIFEST.json"))
    man = json.loads(man_p.read_text())
    part = root / man["parts"][0]["path"]
    lines = part.read_bytes().splitlines(keepends=True)[1:]  # drop one trained-on row
    part.write_bytes(b"".join(lines))
    man["parts"][0]["oid"] = hashlib.sha256(part.read_bytes()).hexdigest()
    man_p.write_text(json.dumps(man))
    with pytest.raises(ValueError, match="Merkle root or row count"):
        read_snapshots(root)


def test_read_snapshots_fails_closed_when_a_row_is_changed(tmp_path):
    import hashlib

    root, _ = _written(tmp_path)
    man_p = next((root / "data" / "snapshots").glob("*/MANIFEST.json"))
    man = json.loads(man_p.read_text())
    part = root / man["parts"][0]["path"]
    rows = [json.loads(x) for x in part.read_text().splitlines()]
    rows[0]["interaction_id"] = "someone-else"
    part.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
    man["parts"][0]["oid"] = hashlib.sha256(part.read_bytes()).hexdigest()
    man_p.write_text(json.dumps(man))
    with pytest.raises(ValueError, match="row_hash"):
        read_snapshots(root)


# ---------------------------------------------------------------- 9. one asset, one synthetic flag


def test_a_real_build_cannot_follow_a_synthetic_build_in_the_same_asset(tmp_path):
    root, _ = _written(tmp_path)
    assert (root / "evals" / "SYNTHETIC.md").exists()
    real = [dict(r, synthetic=False, interaction_id="real-" + r["interaction_id"].removeprefix("syn-"),
                 verdict_id="v" + r["interaction_id"], outcome_id=None)
            for r in _resolve(events("interactions"), events("verdicts")) if r["use"] != "quarantine"]
    out = _build(real, suffix="real")
    with pytest.raises(ValueError, match="one asset, one synthetic flag"):
        out.write(root)
    assert not (root / "data" / "snapshots" / "2026-11-01-real").exists()


# ---------------------------------------------------------------- 2 and 7. CLI: tombstones and every sealed holdout


def _events_with(ev, change):
    ints = events("interactions")
    change(ints)
    (ev / "interactions.jsonl.gz").unlink()
    (ev / "interactions.jsonl").write_text("".join(json.dumps(x) + "\n" for x in ints))
    return ints


def _snap_ids(root, o):
    return {json.loads(x)["interaction_id"] for p in o["snapshot"]["parts"] for x in (root / p["path"]).read_text().splitlines()}


def test_cli_leaves_out_a_tombstoned_subject(tmp_path):
    root, ev = _asset(tmp_path)

    def erase(ints):
        for i in ints[:2]:
            i["subject_token"] = "tok_7f3a91c2"  # the subject in spec/examples/pass/data/tombstones.jsonl

    ints = _events_with(ev, erase)
    r = CliRunner().invoke(app, [*ARGS, "--asset", str(root), "--events", str(ev), "--json"])
    assert r.exit_code == 0, r.output
    o = json.loads(r.output)
    assert o["erased_subjects"] == 1
    assert not {i["interaction_id"] for i in ints[:2]} & _snap_ids(root, o)
    assert o["resolve"]["rows"] == len(ints) - 2  # erased before resolve


def test_cli_excludes_every_sealed_holdout_not_only_the_configured_one(tmp_path):
    root, ev = _asset(tmp_path)
    first = CliRunner().invoke(app, [*ARGS, "--asset", str(root), "--events", str(ev), "--holdout-out", str(tmp_path / "vik"), "--json"])
    assert first.exit_code == 0, first.output
    cand = (tmp_path / "vik" / "candidate.jsonl").read_bytes()
    import hashlib

    d = root / "data" / "holdout" / "H-A"
    d.mkdir()
    (d / "holdout.jsonl").write_bytes(cand)
    (d / "SEAL.json").write_text(json.dumps({"sha256": hashlib.sha256(cand).hexdigest(), "synthetic": True}))
    shutil.rmtree(root / "data" / "snapshots")  # forget the first snapshot: only the new seal keeps its rows out
    r = CliRunner().invoke(app, [*ARGS, "--suffix", "c", "--asset", str(root), "--events", str(ev), "--json"])
    assert r.exit_code == 0, r.output
    o = json.loads(r.output)
    held = {json.loads(x)["id"] for x in cand.decode().splitlines()}
    golden = {json.loads(x)["id"] for x in (root / "evals" / "golden.jsonl").read_text().splitlines()}
    assert o["report"]["counts"]["excluded_by_seal"] >= len(held)
    assert not held & _snap_ids(root, o) and not held & golden


def test_cli_refuses_a_holdout_that_does_not_match_its_seal(tmp_path):
    root, ev = _asset(tmp_path)
    p = root / "data" / "holdout" / "H-2026-10-01" / "holdout.jsonl"
    p.write_text(p.read_text().splitlines()[0] + "\n")
    r = CliRunner().invoke(app, [*ARGS, "--asset", str(root), "--events", str(ev)])
    assert r.exit_code == 1 and "does not match the sha256 in SEAL.json" in r.output


# ================================================================ round 4


def _seen_keys(rows) -> set[tuple[str, str, str]]:
    return {(r.get("interaction_id") or r["id"], r["input_ref"], r["row_hash"]) for r in rows}


def _with_fresh(n: int, tag: str):
    ints, vds = fresh(n, tag)
    return _resolve(events("interactions") + ints, events("verdicts") + vds, events("outcomes"))


# ---------------------------------------------------------------- major: golden and correction-test rows became holdout rows


def test_rows_of_golden_and_correction_tests_never_become_holdout_rows_later(tmp_path):
    rows = _resolve(events("interactions"), events("verdicts"), events("outcomes"))
    first = _build(rows)
    first.write(tmp_path)
    seen = _seen_keys(first.golden) | _seen_keys(first.correction_tests)
    second = _build(_with_fresh(400, "a"), sealed_holdout=first.holdout_candidate.rows, trained_on=read_snapshots(tmp_path),
                    seen=read_seen(tmp_path), suffix="b")
    hold2 = second.holdout_candidate.rows
    assert hold2
    assert not _seen_keys(hold2) & seen
    assert not ({r["id"] for r in hold2} & {k[0] for k in seen} or {r["input_ref"] for r in hold2} & {k[1] for k in seen})
    p = second.proof["pairs"]["holdout-seen"]
    assert p["rows"][1] == len(read_seen(tmp_path)) >= len(seen)
    assert p["shared_interactions"] == p["shared_inputs"] == p["shared_row_hashes"] == p["near_pairs"] == 0
    assert second.report["counts"]["seen_before"] >= len(seen)


def test_a_second_build_on_the_same_rows_refuses_rather_than_holding_out_the_golden_set(tmp_path):
    """The verifier's reproduction: with nothing new, the only unpinned rows were the first golden set."""
    rows = _resolve(events("interactions"), events("verdicts"), events("outcomes"))
    first = _build(rows)
    first.write(tmp_path)
    with pytest.raises(ValueError, match="no rows are eligible for the holdout"):
        _build(rows, sealed_holdout=first.holdout_candidate.rows, trained_on=read_snapshots(tmp_path), seen=read_seen(tmp_path), suffix="b")


def test_rows_dropped_from_golden_between_builds_stay_excluded(tmp_path):
    first = _build(_resolve(events("interactions"), events("verdicts"), events("outcomes")))
    first.write(tmp_path)
    golden1 = _seen_keys(first.golden)
    second = build(_with_fresh(300, "a"), slices(), EvalPolicy(), SEED + 3, as_of=AS_OF, output_schema=output_schema(),
                   sealed_holdout=first.holdout_candidate.rows, trained_on=read_snapshots(tmp_path), seen=read_seen(tmp_path), suffix="b")
    second.write(tmp_path)
    dropped = golden1 - _seen_keys(second.golden)
    assert dropped, "the probe needs golden rows of build 1 that build 2 did not keep in golden"
    record = _seen_keys(read_seen(tmp_path))
    assert golden1 <= record and _seen_keys(second.golden) <= record  # append-only
    firsts = {r["first_seen"] for r in read_seen(tmp_path)}
    assert firsts == {"S-2026-11-01", "S-2026-11-01-b"}
    third = _build(_with_fresh(600, "a"), sealed_holdout=first.holdout_candidate.rows, trained_on=read_snapshots(tmp_path),
                   seen=read_seen(tmp_path), suffix="c")
    assert third.holdout_candidate.rows
    assert not _seen_keys(third.holdout_candidate.rows) & record
    assert not {r["id"] for r in third.holdout_candidate.rows} & {k[0] for k in dropped}


def test_write_refuses_a_candidate_that_holds_a_row_seen_in_an_eval_set(tmp_path):
    """A library caller that forgets `seen` cannot write the build: the candidate would leak at seal time."""
    rows = _resolve(events("interactions"), events("verdicts"), events("outcomes"))
    first = _build(rows)
    first.write(tmp_path)
    second = _build(rows, sealed_holdout=first.holdout_candidate.rows, trained_on=read_snapshots(tmp_path), suffix="b")
    assert _seen_keys(second.holdout_candidate.rows) & _seen_keys(first.golden)
    with pytest.raises(LeakageError, match="seen"):
        second.write(tmp_path)
    assert not (tmp_path / "data" / "snapshots" / "2026-11-01-b").exists()
    assert _seen_keys(read_seen(tmp_path)) == _seen_keys(first.golden) | _seen_keys(first.correction_tests)


def _append_fresh(ev, n, tag):
    ints, vds = fresh(n, tag)
    for name, extra in (("interactions", ints), ("verdicts", vds)):
        with gzip.open(ev / f"{name}.jsonl.gz", "rt", encoding="utf-8") as f:
            have = [json.loads(x) for x in f if x.strip()]
        (ev / f"{name}.jsonl.gz").unlink()
        (ev / f"{name}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in have + extra))
    return {i["interaction_id"] for i in ints}


def _eval_keys(root):
    g = [json.loads(x) for x in (root / "evals" / "golden.jsonl").read_text().splitlines()]
    c = [json.loads(x) for x in (root / "evals" / "correction_tests.jsonl").read_text().splitlines()]
    return _seen_keys(g) | _seen_keys(c)


def test_cli_second_build_never_holds_out_golden_or_correction_test_rows_of_the_first(tmp_path):
    root, ev = _asset(tmp_path)
    legacy = _eval_keys(root)  # the example asset's own eval rows, from before the record existed
    first = CliRunner().invoke(app, [*ARGS, "--asset", str(root), "--events", str(ev), "--holdout-out", str(tmp_path / "v1"), "--json"])
    assert first.exit_code == 0, first.output
    seen1 = _eval_keys(root)
    assert "evals/seen.jsonl" in json.loads(first.output)["files"]
    assert _seen_keys(read_seen(root)) == legacy | seen1
    cand = (tmp_path / "v1" / "candidate.jsonl").read_bytes()
    d = root / "data" / "holdout" / "H-A"
    d.mkdir()
    (d / "holdout.jsonl").write_bytes(cand)
    (d / "SEAL.json").write_text(json.dumps({"sha256": hashlib.sha256(cand).hexdigest(), "synthetic": True}))
    new = _append_fresh(ev, 300, "cli")
    second = CliRunner().invoke(app, [*ARGS, "--suffix", "b", "--asset", str(root), "--events", str(ev), "--holdout-out", str(tmp_path / "v2"), "--json"])
    assert second.exit_code == 0, second.output
    o = json.loads(second.output)
    hold2 = [json.loads(x) for x in (tmp_path / "v2" / "candidate.jsonl").read_text().splitlines()]
    assert hold2 and {r["id"] for r in hold2} <= new
    assert not _seen_keys(hold2) & (legacy | seen1)
    assert o["seen_rows"] == len(legacy | seen1) and o["proof"]["pairs"]["holdout-seen"]["shared_inputs"] == 0
    assert _seen_keys(read_seen(root)) == legacy | seen1 | _eval_keys(root)


# ---------------------------------------------------------------- minor 2: trained_on keyed on an unchecked row_hash


def test_trained_on_rows_with_empty_hashes_still_pin_every_row():
    rows = _resolve(events("interactions"), events("verdicts"), events("outcomes"))
    first = _build(rows)
    trained = {r["interaction_id"] for r in first.snapshot.rows}
    nxt = _build(rows, trained_on=[dict(r, row_hash="") for r in first.snapshot.rows], suffix="b")
    held = {r["id"] for r in nxt.holdout_candidate.rows} | {r["id"] for r in nxt.golden}
    assert held and not held & trained
    assert nxt.proof["pairs"]["holdout-trained_on"]["rows"][1] == len(trained)
    assert nxt.report["counts"]["trained_before"] >= len(trained)


def test_trained_on_rows_with_a_forged_hash_are_refused():
    rows = _resolve(events("interactions"), events("verdicts"), events("outcomes"))
    first = _build(rows)
    forged = [dict(r, row_hash="a" * 64) for r in first.snapshot.rows]
    with pytest.raises(ValueError, match="row_hash"):
        _build(rows, trained_on=forged, suffix="b")


# ---------------------------------------------------------------- minor 3: the synthetic flag of a sealed holdout


def _real_rows():
    return [dict(r, synthetic=False, interaction_id="real-" + r["interaction_id"].removeprefix("syn-"),
                 verdict_id="v" + r["interaction_id"], outcome_id=None)
            for r in _resolve(events("interactions"), events("verdicts")) if r["use"] != "quarantine"]


def test_a_real_build_cannot_go_into_an_asset_with_only_a_synthetic_sealed_holdout(tmp_path):
    root = tmp_path / "asset"
    syn = _build(_resolve(events("interactions"), events("verdicts")))
    seal_candidate(syn.holdout_candidate, root, "H-syn", sealed_by="vik", sealed_at="2026-11-01T09:02:00Z", audit_seq=1,
                   store=ObjectStore(tmp_path / "bucket"))
    assert not (root / "data" / "snapshots").exists() and not (root / "evals").exists()
    out = _build(_real_rows(), suffix="real")
    with pytest.raises(ValueError, match="one asset, one synthetic flag"):
        out.write(root)
    assert not (root / "data" / "snapshots").exists() and not (root / "evals").exists()


def test_build_refuses_rows_whose_flag_differs_from_the_seals():
    with pytest.raises(ValueError, match="one asset, one synthetic flag"):
        _build(_real_rows(), sealed_synthetic=True, suffix="real")
    assert _build(_real_rows(), sealed_synthetic=False, suffix="real").report["synthetic"] is False


def test_read_sealed_surfaces_the_synthetic_flag_and_refuses_disagreeing_seals(tmp_path):
    root, _ = _asset(tmp_path)
    rows, flag = read_sealed(root, "data/holdout/H-2026-11-01")
    assert rows and flag is True
    seal = root / "data" / "holdout" / "H-2026-10-01" / "SEAL.json"
    seal.write_text(json.dumps({**json.loads(seal.read_text()), "synthetic": False}))
    with pytest.raises(ValueError, match="synthetic flag"):
        read_sealed(root, "data/holdout/H-2026-11-01")
