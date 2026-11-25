from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from typer.testing import CliRunner

from cli_asset import _cache_dir, asset  # noqa: F401  (pytest fixtures)
from roadbed.cli import rebase as rebase_mod
from roadbed.cli.asset import open_asset, receipt_executor
from roadbed.cli.main import app
from roadbed.contracts import GateResult, Scores, json_schema
from roadbed.io import read_yaml, write_yaml

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
    assert r.exit_code == 4, r.output  # a blocked gate exits 4 (Day 0: 3)
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
    assert r2.exit_code == 4 and _last(r2.output) == BLOCK_LINE
    assert sorted(p.name for p in (asset / "findings").iterdir()) == ["F-0001.yaml", "F-0002.yaml", "F-0003.yaml"]


def test_rebase_json(asset: Path):
    r = _rebase(asset, 14, "--json")
    assert r.exit_code == 4
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
    assert r.exit_code == 2 and "does not match" in r.output


def test_rebase_license_denied(asset: Path):
    r = _rebase(asset, 13, "--license", "llama3")
    assert r.exit_code == 4
    assert _last(r.output).startswith("blocked: license llama3 denied")


def test_rebase_base_from_watch_cache(asset: Path, _cache_dir: Path):
    (asset / "rebases/13/rebase.yaml").unlink()
    r = _rebase(asset, 13)
    assert r.exit_code == 2 and "no base model" in r.output
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


# ---------------------------------------------------------------- honesty: synthetic marks and executor

EXAMPLES = Path(__file__).resolve().parents[4] / "spec" / "examples"


def _real(asset: Path) -> None:
    """Turn the test asset into a real one: every synthetic flag false, no SYNTHETIC in the tenant name."""
    for rel in ("roadbed.yaml", "evals/scorers.yaml", "evals/slices.yaml", "policy/gate.yaml", "policy/license.yaml",
                "rebases/13/rebase.yaml", "rebases/14/rebase.yaml"):
        doc = read_yaml(asset / rel)
        doc["synthetic"] = False
        if rel == "roadbed.yaml":
            doc["tenant"] = "Acme Mutual"
        write_yaml(asset / rel, doc)
    (asset / "SYNTHETIC.md").unlink()
    for f in (asset / "findings").iterdir():
        write_yaml(f, {**read_yaml(f), "synthetic": False})


def _receipt(asset: Path, n: int, executor: str) -> None:
    doc = json.loads((EXAMPLES / "pass/rebases/13/receipt.json").read_text())
    doc["predicate"]["executor"]["name"] = executor
    (asset / f"rebases/{n}/receipt.json").write_text(json.dumps(doc, indent=2))


@pytest.mark.parametrize("name,n", [("pass", 13), ("blocked", 14)])
def test_fixture_reports_render_as_before(name: str, n: int):
    """The synthetic fixtures (mock receipts) keep the Day 0 text byte for byte."""
    a = open_asset(EXAMPLES / name)
    d = a.root / f"rebases/{n}"
    meta = read_yaml(d / "rebase.yaml")
    base = rebase_mod.BaseRef(meta["base"]["repo"], meta["base"]["revision"], meta["base"].get("license"))
    scores = Scores.model_validate_json((d / "scores.json").read_text())
    gate = GateResult.model_validate_json((d / "gate.json").read_text())
    final = rebase_mod.final_line(n, gate, a.policy.margin)
    got = rebase_mod.report_md(a, n, base, scores, gate, final, receipt_executor(d))
    assert got == (d / "report.md").read_text(encoding="utf-8")
    assert got.splitlines()[0].endswith("(SYNTHETIC)")


@pytest.mark.parametrize("n,line", [(13, PASS_LINE), (14, BLOCK_LINE)])
def test_real_asset_report_has_no_synthetic_mark(asset: Path, n: int, line: str):
    _real(asset)
    r = _rebase(asset, n)
    assert r.exit_code in (0, 4), r.output
    d = asset / f"rebases/{n}"
    report = (d / "report.md").read_text()
    assert "SYNTHETIC" not in report.upper()
    lines = report.splitlines()
    assert lines[0] == f"# Rebase #{n}: Qwen/Qwen3.8-27B for claims-triage"
    assert lines[2].startswith("> **Executor: none, predictions supplied with `--from-predictions`") and "Data: real." in lines[2]
    assert report.rstrip().endswith(line)
    assert read_yaml(d / "rebase.yaml")["synthetic"] is False
    assert json.loads((d / "scores.json").read_text())["synthetic"] is False
    for f in (asset / "findings").iterdir():
        assert read_yaml(f)["synthetic"] is False
    if n == 13:
        assert "SYNTHETIC" not in (d / "roadbed.yaml.proposed").read_text()


def test_runner_receipt_says_runner_eval_job(asset: Path):
    _real(asset)
    _receipt(asset, 13, "runner")
    r = _rebase(asset, 13)
    assert r.exit_code == 0, r.output
    d = asset / "rebases/13"
    report = (d / "report.md").read_text()
    assert report.splitlines()[2].startswith("> **Executor: runner, predictions from the runner eval job. Data: real.**")
    assert "predictions supplied" not in report and "SYNTHETIC" not in report.upper()
    assert json.loads((d / "scores.json").read_text())["notice"] == "predictions from the runner eval job"
    assert "predictions from the runner eval job" in r.output and "no training in this run" not in r.output


def test_synthetic_asset_with_runner_receipt_says_both(asset: Path):
    _receipt(asset, 13, "runner")
    r = _rebase(asset, 13)
    assert r.exit_code == 0, r.output
    lines = (asset / "rebases/13/report.md").read_text().splitlines()
    assert lines[0] == "# Rebase #13: Qwen/Qwen3.8-27B for claims-triage"
    assert lines[2].startswith("> **Executor: runner, predictions from the runner eval job. Data: SYNTHETIC")


def test_mock_receipt_on_real_asset_says_supplied(asset: Path):
    _real(asset)
    _receipt(asset, 13, "mock")
    assert _rebase(asset, 13).exit_code == 0
    line = (asset / "rebases/13/report.md").read_text().splitlines()[2]
    assert line.startswith("> **Executor: mock, predictions supplied, no model was trained. Data: real.**")
