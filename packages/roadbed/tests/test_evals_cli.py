"""W14 CLI hook: roadbed evals build (for W21)."""

from __future__ import annotations

import json
import shutil

from test_evals_support import FIXTURE, SCHEMAS
from typer.testing import CliRunner

from roadbed.asset import validate_file
from roadbed.cli.main import app

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
    assert r.exit_code == 1 and "never into the asset" in r.output
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
    assert r.exit_code == 1 and "neither in the asset nor in the bucket" in r.output


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
