"""Inputs that the SDK tokenised (text="tokenize", the default) hold no text to compare. The near-duplicate check cannot
run on them: evals build warns and records it in build.json, and verify reports a named skip (so --strict fails),
never a pass with "max 0.0"."""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from test_evals_support import FIXTURE, events, input_ref
from typer.testing import CliRunner

from roadbed.asset.overlap import is_tokenised
from roadbed.cli.main import app
from test_asset_support import LOCAL_HID, local_asset

cli = CliRunner()
REASON = 'near-duplicate check not possible: inputs are tokens, not text (SDK text="tokenize")'


def tokenised_events(dest: Path) -> Path:
    """The acme-2k events, with each narrative replaced by the token the SDK writes in text="tokenize" mode."""
    dest.mkdir(parents=True)
    for name in ("verdicts", "outcomes"):
        shutil.copy(FIXTURE / f"{name}.jsonl.gz", dest / f"{name}.jsonl.gz")
    rows = events("interactions")
    for r in rows:
        r["input"]["narrative"] = "<TEXT:tok_" + hashlib.sha256(r["input"]["narrative"].encode()).hexdigest()[:32] + ">"
        r["input_ref"] = input_ref(r["input"])
    with gzip.open(dest / "interactions.jsonl.gz", "wt", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    return dest


@pytest.fixture(scope="module")
def tok_asset(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("tok")
    return local_asset(d, rebase=True, events=tokenised_events(d / "events"))


TOK = "<TEXT:tok_0123456789abcdef0123456789abcdef>"


@pytest.mark.parametrize(("text", "want"), [
    (json.dumps({"messages": [{"content": TOK, "role": "user"}]}), True),
    (json.dumps({"narrative": TOK, "line": "auto_physical", "state": "ca"}), True),
    (json.dumps(TOK), True),
    (json.dumps({"narrative": "<PERSON_1> slid on ice into a guardrail", "line": "auto_physical"}), False),
    (json.dumps({"narrative": f"{TOK} and more words", "line": "auto_physical"}), False),
    (json.dumps({"line": "auto_physical", "state": "ca"}), False),
])
def test_is_tokenised_when_tokens_are_the_only_free_text(text, want):
    assert is_tokenised(text) is want


def _build(tmp_path: Path, ev: Path):
    root = tmp_path / "acme"
    r = cli.invoke(app, ["new", str(root), "--model", "claims-triage", "--synthetic", "--slices", str(FIXTURE / "slices.yaml"),
                         "--output-schema", str(FIXTURE / "output.schema.json")])
    assert r.exit_code == 0, r.output
    r = cli.invoke(app, ["evals", "build", "--asset", str(root), "--events", str(ev), "--as-of", "2026-11-02T09:00:00Z", "--seed", "7"],
                   env={"COLUMNS": "300"})
    assert r.exit_code == 0, r.output
    report = json.loads((root / "data/snapshots/2026-11-02/build.json").read_text())["report"]
    return r.output, report


def test_build_warns_and_records_that_the_near_check_cannot_run_on_tokens(tmp_path):
    out, report = _build(tmp_path, tokenised_events(tmp_path / "events"))
    assert REASON in out and 'use text="redact" for text that goes to training' in out
    assert "max Jaccard" not in out
    assert report["near_check"] == "not_applicable_tokenised"
    assert report["counts"]["tokenised_inputs"] == report["counts"]["resolved"] - report["counts"]["quarantined"] > 0


def test_build_on_readable_text_runs_the_near_check(tmp_path):
    out, report = _build(tmp_path, FIXTURE)
    assert REASON not in out and "max Jaccard" in out
    assert report["near_check"] == "jaccard" and report["counts"]["tokenised_inputs"] == 0


@pytest.mark.parametrize("target", [f"data/holdout/{LOCAL_HID}", "rebases/1"])
def test_verify_reports_the_near_check_as_a_named_skip_on_tokens(tok_asset, target):
    objects = str(tok_asset.parent / "objects")
    r = cli.invoke(app, ["verify", str(tok_asset / target), "--objects", objects, "--asset", str(tok_asset)], env={"COLUMNS": "300"})
    near = [ln for ln in r.output.splitlines() if "disjoint: near" in ln]
    assert len(near) == 1 and "skipped" in near[0] and REASON in near[0], r.output
    assert "max 0.0" not in r.output


def test_verify_strict_fails_on_tokens(tok_asset):
    r = cli.invoke(app, ["verify", str(tok_asset / f"data/holdout/{LOCAL_HID}"), "--objects", str(tok_asset.parent / "objects"),
                         "--asset", str(tok_asset), "--strict"], env={"COLUMNS": "300"})
    assert r.exit_code == 4, r.output
    assert REASON in r.output and "--strict" in r.output
