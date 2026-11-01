from __future__ import annotations

import json
from pathlib import Path

import jsonschema
from typer.testing import CliRunner

from roadbed.cli.main import app

from cli_asset import _cache_dir, asset  # noqa: F401  (pytest fixtures)
from roadbed.contracts import json_schema
from roadbed.io import read_yaml

runner = CliRunner()
PASS_LINE = "opened rebase #13 → awaiting MRM sign-off"
BLOCK_LINE = "blocked: line=bodily_injury −20.0 below −2.0 margin · F-0003 logged"
SCHEMAS = Path(__file__).resolve().parents[4] / "spec" / "schemas" / "v0.1"


def _rebase(asset: Path, n: int, *extra: str):
    return runner.invoke(app, ["rebase", "claims-triage", "--from-predictions", f"rebases/{n}",
                               "--asset", str(asset), *extra])


def _last(out: str) -> str:
    return out.strip().splitlines()[-1]


def _validate(path: Path, name: str) -> dict:
    data = json.loads(path.read_text())
    jsonschema.Draft202012Validator(json_schema(name)).validate(data)
    on_disk = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
    jsonschema.Draft202012Validator(on_disk).validate(data)
    return data


def test_rebase_pass(asset: Path):
    before = (asset / "roadbed.yaml").read_bytes()
    r = _rebase(asset, 13)
    assert r.exit_code == 0, r.output
    assert _last(r.output) == PASS_LINE
    d = asset / "rebases/13"
    scores = _validate(d / "scores.json", "scores")
    gate = _validate(d / "gate.json", "gate")
    assert scores["synthetic"] is True and gate["synthetic"] is True and gate["status"] == "pass"
    assert scores["notice"] == "predictions supplied via --from-predictions; no training in this run"
    assert scores["holdout"]["rows"] == 60
    assert scores["predictions"]["candidate"]["path"] == "rebases/13/predictions.candidate.jsonl"
    out = r.output
    assert "new base: Qwen/Qwen3.8-27B" in out
    assert "predictions supplied via --from-predictions; no training in this run" in out
    assert "scored on 60 sealed holdout cases" in out
    assert "trained on" not in out
    o = scores["overall"]
    assert o["delta"] > 0 and o["ci_low"] > 0
    assert "(CI +" in out and "0 of 11 slices below −2.0 margin · 10 flagged low-n" in out
    # proposed base bump, live roadbed.yaml untouched
    assert (asset / "roadbed.yaml").read_bytes() == before
    prop = read_yaml(d / "roadbed.yaml.proposed")
    assert prop["base"] == {"repo": "Qwen/Qwen3.8-27B", "revision": "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"}
    assert prop["alias"] == {"name": "claims-triage-prod", "version": "v12"}
    report = (d / "report.md").read_text()
    assert report.startswith("# Rebase #13") and report.rstrip().endswith(PASS_LINE)
    assert "Illustrative: predictions supplied, no training in this run" in report
    manifest = (d / "MANIFEST.sha256").read_text()
    for name in ("data/holdout/H-TEST/holdout.jsonl", "rebases/13/predictions.candidate.jsonl",
                 "rebases/13/predictions.incumbent.jsonl", "rebases/13/scores.json", "rebases/13/gate.json",
                 "rebases/13/report.md", "rebases/13/rebase.yaml", "rebases/13/roadbed.yaml.proposed"):
        assert f"  {name}\n" in manifest


def test_rebase_blocked(asset: Path):
    before = (asset / "roadbed.yaml").read_bytes()
    r = _rebase(asset, 14)
    assert r.exit_code == 3, r.output
    assert _last(r.output) == BLOCK_LINE
    d = asset / "rebases/14"
    gate = _validate(d / "gate.json", "gate")
    _validate(d / "scores.json", "scores")
    assert gate["status"] == "blocked" and [f["id"] for f in gate["findings"]] == ["F-0003"]
    f = read_yaml(asset / "findings/F-0003.yaml")
    assert f["slice"] == "line=bodily_injury" and f["rebase"] == 14 and f["n"] == 30 and f["synthetic"] is True
    assert not (d / "roadbed.yaml.proposed").exists()
    assert (asset / "roadbed.yaml").read_bytes() == before
    assert (d / "report.md").read_text().rstrip().endswith(BLOCK_LINE)
    # rerun is idempotent: same finding id, no duplicates
    r2 = _rebase(asset, 14)
    assert r2.exit_code == 3 and _last(r2.output) == BLOCK_LINE
    assert sorted(p.name for p in (asset / "findings").iterdir()) == ["F-0001.yaml", "F-0002.yaml", "F-0003.yaml"]


def test_rebase_json(asset: Path):
    r = _rebase(asset, 14, "--json")
    assert r.exit_code == 3
    data = json.loads(r.output)
    assert data["status"] == "blocked" and data["final"] == BLOCK_LINE and data["findings"] == ["F-0003"]
    assert data["synthetic"] is True
    assert [e["event"] for e in data["events"]][:2] == [
        "new base: Qwen/Qwen3.8-27B", "predictions supplied via --from-predictions; no training in this run"]


def test_rebase_cwd_and_model_mismatch(asset: Path, monkeypatch):
    monkeypatch.chdir(asset / "rebases")
    r = runner.invoke(app, ["rebase", "claims-triage", "--from-predictions", "rebases/13"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["rebase", "other-model", "--from-predictions", "rebases/13"])
    assert r.exit_code == 1 and "does not match" in r.output


def test_rebase_license_denied(asset: Path):
    r = _rebase(asset, 13, "--license", "llama3")
    assert r.exit_code == 3
    assert _last(r.output).startswith("blocked: license llama3 denied")


def test_rebase_base_from_watch_cache(asset: Path, _cache_dir: Path):
    (asset / "rebases/13/rebase.yaml").unlink()
    r = _rebase(asset, 13)
    assert r.exit_code == 1 and "no base model" in r.output
    _cache_dir.mkdir(parents=True)
    (_cache_dir / "watch.json").write_text(json.dumps({"last": "Qwen/Qwen3.*", "entries": {"Qwen/Qwen3.*": {
        "glob": "Qwen/Qwen3.*", "repo": "Qwen/Qwen3.8-27B", "sha": "a" * 40, "license": "apache-2.0",
        "last_modified": None, "fetched_at": "2026-11-01T09:00:00Z"}}}))
    r = _rebase(asset, 13)
    assert r.exit_code == 0, r.output
    meta = read_yaml(asset / "rebases/13/rebase.yaml")
    assert meta["base"]["revision"] == "a" * 40 and meta["created_at"]


def test_status(asset: Path):
    _rebase(asset, 13)
    _rebase(asset, 14)
    r = runner.invoke(app, ["status", "--asset", str(asset)])
    assert r.exit_code == 0, r.output
    assert "claims-triage" in r.output and "claims-triage-prod v12" in r.output and "Qwen/Qwen3.6-27B" in r.output
    assert "#13" in r.output and "pass" in r.output and "#14" in r.output and "blocked" in r.output
    data = json.loads(runner.invoke(app, ["status", "--asset", str(asset), "--json"]).output)
    assert [(x["rebase"], x["status"]) for x in data["rebases"]] == [(13, "pass"), (14, "blocked")]
    assert data["rebases"][1]["findings"] == ["F-0003"]
