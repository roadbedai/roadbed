"""roadbed new: scaffold an asset that validates in closed form and that evals build accepts."""

from __future__ import annotations

import json

import yaml
from test_evals_support import FIXTURE
from typer.testing import CliRunner

from roadbed.asset import validate_file
from roadbed.asset.files import schema_for, tree_files
from roadbed.cli.asset import open_asset
from roadbed.cli.main import app
from roadbed.contracts import SCHEMA_MODELS

cli = CliRunner()
FILES = ["evals/output.schema.json", "evals/scorers.yaml", "evals/slices.yaml", "policy/gate.yaml", "policy/license.yaml",
         "roadbed.yaml"]


def _new(tmp_path, *extra: str, slices=FIXTURE / "slices.yaml"):
    return cli.invoke(app, ["new", str(tmp_path / "acme"), "--model", "claims-triage", "--slices", str(slices),
                            "--output-schema", str(FIXTURE / "output.schema.json"), *extra])


def _real_slices(tmp_path):
    doc = yaml.safe_load((FIXTURE / "slices.yaml").read_text())
    del doc["synthetic"]
    p = tmp_path / "slices.yaml"
    p.write_text(yaml.safe_dump(doc))
    return p


def test_every_file_validates_in_closed_form(tmp_path):
    r = _new(tmp_path, "--synthetic")
    assert r.exit_code == 0, r.output
    root = tmp_path / "acme"
    assert sorted(tree_files(root)) == sorted([*FILES, "SYNTHETIC.md"])
    for rel in FILES:
        if schema_for(rel):
            validate_file(root, rel, strict=True)
    a = open_asset(root)
    assert a.cfg["data"] == {"verdicts": "data/snapshots"}
    assert a.cfg["base"] == {"repo": "unset", "revision": "unpinned"} and a.cfg["alias"] == {"name": "claims-triage-prod", "version": "unset"}
    assert (a.policy.margin, a.policy.min_n, a.policy.contamination.num_perm) == (-2.0, 30, 128)  # type: ignore[union-attr]
    assert [(f.field, f.weight) for f in a.scorers.fields] == [("route", 1.0), ("severity", 1.0)]
    assert (root / "evals/output.schema.json").read_bytes() == (FIXTURE / "output.schema.json").read_bytes()


def test_synthetic_flag_on_every_file_whose_schema_has_it(tmp_path):
    assert _new(tmp_path, "--synthetic").exit_code == 0
    root = tmp_path / "acme"
    flagged = []
    for rel in FILES:
        name = schema_for(rel)
        if name and "synthetic" in SCHEMA_MODELS[name].model_fields:
            assert yaml.safe_load((root / rel).read_text())["synthetic"] is True, rel
            flagged.append(rel)
    assert len(flagged) == 5 and (root / "SYNTHETIC.md").is_file()


def test_real_asset_has_no_synthetic_flag(tmp_path):
    r = _new(tmp_path, "--tenant", "Acme", "--base", "Qwen/Qwen3-8B", "--margin", "-1.5", "--min-n", "20",
             slices=_real_slices(tmp_path))
    assert r.exit_code == 0, r.output
    root = tmp_path / "acme"
    assert not (root / "SYNTHETIC.md").exists()
    cfg = open_asset(root).cfg
    assert cfg["synthetic"] is False and cfg["tenant"] == "Acme" and cfg["base"]["repo"] == "Qwen/Qwen3-8B"
    assert (cfg["evals"]["margin"], cfg["evals"]["min_n"]) == (-1.5, 20)
    for rel in FILES:
        if rel.endswith(".yaml"):
            assert "synthetic" not in yaml.safe_load((root / rel).read_text()) or rel == "roadbed.yaml", rel


def test_synthetic_slices_need_the_synthetic_flag(tmp_path):
    r = _new(tmp_path)
    assert r.exit_code == 2 and "--synthetic" in r.output
    assert not (tmp_path / "acme").exists()


def test_existing_directory_is_refused(tmp_path):
    (tmp_path / "acme").mkdir()
    (tmp_path / "acme" / "keep.txt").write_text("x")
    r = _new(tmp_path, "--synthetic")
    assert r.exit_code == 2 and "exists" in r.output
    assert [p.name for p in (tmp_path / "acme").iterdir()] == ["keep.txt"]


def test_prints_the_git_line_and_runs_no_git(tmp_path):
    r = _new(tmp_path, "--synthetic")
    assert "git init -q && git add -A && git commit" in r.output
    assert not (tmp_path / "acme" / ".git").exists()
    r = _new(tmp_path / "j", "--synthetic", "--json")
    o = json.loads(r.output)
    assert o["ok"] and sorted(o["files"]) == sorted([*FILES, "SYNTHETIC.md"])


def test_evals_build_accepts_the_scaffold(tmp_path):
    assert _new(tmp_path, "--synthetic").exit_code == 0
    r = cli.invoke(app, ["evals", "build", "--asset", str(tmp_path / "acme"), "--events", str(FIXTURE),
                         "--as-of", "2026-11-02T09:00:00Z", "--seed", "7", "--holdout-out", str(tmp_path / "vault"), "--json"])
    assert r.exit_code == 0, r.output
    for rel in json.loads(r.output)["files"]:
        validate_file(tmp_path / "acme", rel, strict=True)
