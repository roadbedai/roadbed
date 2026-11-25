"""CLI hook: roadbed evals build."""

from __future__ import annotations

import json
import shutil

import pytest
import yaml
from test_evals_support import FIXTURE, SCHEMAS
from typer.testing import CliRunner

from roadbed.asset import validate_file
from roadbed.cli import evals as evals_cli
from roadbed.cli.main import app
from roadbed.evals import LeakageError

PASS = SCHEMAS.parents[1] / "examples" / "pass"


def _asset(tmp_path):
    root = tmp_path / "asset"
    shutil.copytree(PASS, root)
    shutil.rmtree(root / "data" / "snapshots")  # the fixture's parts live in a bucket this test does not have
    shutil.copy(FIXTURE / "slices.yaml", root / "evals" / "slices.yaml")  # the fixture tenant's slices
    ev = tmp_path / "events"
    ev.mkdir()
    for n in ("interactions", "verdicts", "outcomes"):
        shutil.copy(FIXTURE / f"{n}.jsonl.gz", ev / f"{n}.jsonl.gz")
    return root, ev


ARGS = ["evals", "build", "--as-of", "2026-11-02T09:00:00Z", "--seed", "7", "--created-at", "2026-11-02T09:05:00Z"]


def test_evals_build_writes_the_snapshot_and_hands_the_candidate_to_the_validator(tmp_path):
    root, ev = _asset(tmp_path)
    r = CliRunner().invoke(app, [*ARGS, "--asset", str(root), "--events", str(ev), "--holdout-out", str(tmp_path / "vik"), "--json"])
    assert r.exit_code == 0, r.output
    o = json.loads(r.output)
    assert o["ok"] and o["snapshot"]["id"] == "S-2026-11-02" and o["snapshot"]["label_rules"] == "label_rules@v1"
    assert o["holdout_candidate"]["rows"] == o["report"]["counts"]["holdout_candidate"] > 0
    for rel in o["files"]:
        validate_file(root, rel, strict=True)
    assert (tmp_path / "vik" / "candidate.jsonl").is_file()
    assert not list(root.rglob("candidate*"))


def test_evals_build_refuses_a_candidate_dir_inside_the_asset_and_writes_nothing(tmp_path):
    root, ev = _asset(tmp_path)
    r = CliRunner().invoke(app, [*ARGS, "--asset", str(root), "--events", str(ev), "--holdout-out", str(root / "data" / "x")])
    assert r.exit_code == 2 and "never into the asset" in r.output
    assert not (root / "data" / "snapshots" / "2026-11-02").exists()


def test_evals_build_text_output_without_candidate_dir(tmp_path):
    root, ev = _asset(tmp_path)
    r = CliRunner().invoke(app, [*ARGS, "--asset", str(root), "--events", str(ev)])
    assert r.exit_code == 0, r.output
    assert "SYNTHETIC" in r.output and "S-2026-11-02" in r.output and "not written" in r.output


def test_evals_build_refuses_when_an_earlier_snapshot_cannot_be_read(tmp_path):
    root, ev = _asset(tmp_path)
    shutil.copytree(PASS / "data" / "snapshots", root / "data" / "snapshots")  # bucket-only parts, no --objects
    r = CliRunner().invoke(app, [*ARGS, "--asset", str(root), "--events", str(ev)])
    assert r.exit_code == 2 and "neither in the asset nor in the bucket" in r.output


def test_second_build_never_holds_out_what_the_first_snapshot_trained_on(tmp_path):
    root, ev = _asset(tmp_path)
    first = CliRunner().invoke(app, [*ARGS, "--asset", str(root), "--events", str(ev), "--json"])
    assert first.exit_code == 0, first.output
    part = root / json.loads(first.output)["snapshot"]["parts"][0]["path"]
    trained = {json.loads(x)["interaction_id"] for x in part.read_text().splitlines()}
    second = CliRunner().invoke(app, [*ARGS, "--suffix", "b", "--seed", "8", "--asset", str(root), "--events", str(ev),
                                      "--holdout-out", str(tmp_path / "vik"), "--json"])
    assert second.exit_code == 0, second.output
    o = json.loads(second.output)
    assert o["report"]["counts"]["trained_before"] >= len(trained)
    held = {json.loads(x)["id"] for x in (tmp_path / "vik" / "candidate.jsonl").read_text().splitlines()}
    golden = {json.loads(x)["id"] for x in (root / "evals" / "golden.jsonl").read_text().splitlines()}
    assert held and not held & trained and not golden & trained


def test_a_leakage_refusal_exits_4_and_other_failures_exit_2(tmp_path, monkeypatch):
    """Exit codes follow the CLI rules: a leakage refusal is a failed check (4, like roadbed verify); bad input is 2."""
    root, ev = _asset(tmp_path)

    def refuse(*a, **k):
        raise LeakageError("train-holdout not disjoint (threshold 0.9): probe")

    monkeypatch.setattr(evals_cli, "build", refuse)
    r = CliRunner().invoke(app, [*ARGS, "--asset", str(root), "--events", str(ev), "--json"])
    assert r.exit_code == 4, r.output
    o = json.loads(r.output)
    assert o == {"ok": False, "error": "train-holdout not disjoint (threshold 0.9): probe", "exit_code": 4}
    assert not (root / "data" / "snapshots" / "2026-11-02").exists()


def test_objects_store_holds_the_training_inputs_and_no_holdout_input(tmp_path):
    from roadbed.asset import ObjectStore

    root, ev = _asset(tmp_path)
    r = CliRunner().invoke(app, [*ARGS, "--asset", str(root), "--events", str(ev), "--holdout-out", str(tmp_path / "vik"),
                                 "--objects", str(tmp_path / "objects"), "--json"])
    assert r.exit_code == 0, r.output
    store = ObjectStore(tmp_path / "objects")
    part = root / json.loads(r.output)["snapshot"]["parts"][0]["path"]
    train = [json.loads(x) for x in part.read_text().splitlines()]
    assert train and all(store.has(t["input_ref"].removeprefix("sha256:")) for t in train)
    held = [json.loads(x) for x in (tmp_path / "vik" / "candidate.jsonl").read_text().splitlines()]
    assert held and not any(store.has(h["input_ref"].removeprefix("sha256:")) for h in held)


def test_evals_build_refuses_an_objects_store_inside_the_asset_and_writes_nothing(tmp_path, monkeypatch):
    root, ev = _asset(tmp_path)
    monkeypatch.chdir(root)
    for objects in ("./objects", "."):
        r = CliRunner().invoke(app, [*ARGS, "--asset", str(root), "--events", str(ev), "--objects", objects])
        assert r.exit_code == 2 and "--objects must be outside the asset" in r.output
    assert not (root / "objects").exists()
    assert not (root / "data" / "snapshots").exists() and not (root / "evals" / "seen.jsonl").exists()


@pytest.mark.parametrize("field", [("gate",), ("evals", "scorers"), ("evals", "slices"), ("task", "output_schema")])
@pytest.mark.parametrize("kind", ["absolute", "dotdot"])
def test_a_roadbed_yaml_path_outside_the_asset_is_refused_without_echoing_the_file(tmp_path, field, kind):
    root, ev = _asset(tmp_path)
    secret = tmp_path / "outside_secret.yaml"
    secret.write_text('margin: -2.0\nsecret_api_key: "sk-live-THIS-IS-A-SECRET-OUTSIDE-ASSET"\n')
    cfg = yaml.safe_load((root / "roadbed.yaml").read_text())
    node = cfg
    for k in field[:-1]:
        node = node[k]
    node[field[-1]] = str(secret) if kind == "absolute" else "../outside_secret.yaml"
    (root / "roadbed.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    r = CliRunner().invoke(app, [*ARGS, "--asset", str(root), "--events", str(ev)])
    assert r.exit_code == 2, r.output
    assert ".".join(field) in r.output and "relative path inside the asset" in r.output, r.output
    assert "sk-live" not in r.output and "secret_api_key" not in r.output and "margin" not in r.output
