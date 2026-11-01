from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli_asset import _cache_dir, asset  # noqa: F401  (pytest fixtures)
from roadbed.cli.main import app
from roadbed.hashing import sha256_file

runner = CliRunner()


def _prepare(asset: Path) -> None:
    for n in (13, 14):
        runner.invoke(app, ["rebase", "claims-triage", "--from-predictions", f"rebases/{n}", "--asset", str(asset)])


@pytest.mark.parametrize("n", [13, 14])
def test_verify_passes(asset: Path, n: int, monkeypatch):
    _prepare(asset)
    monkeypatch.chdir(asset)
    r = runner.invoke(app, ["verify", f"rebases/{n}"])
    assert r.exit_code == 0, r.output
    for label in ("hashes OK", "schemas OK", "scores recomputed: match", "seal precedes rebase: OK"):
        assert label in r.output
    assert "MOCK" not in r.output and "executor" not in r.output  # Day 0: no receipt
    data = json.loads(runner.invoke(app, ["verify", f"rebases/{n}", "--json"]).output)
    assert data["executors"] == [] and data["mock"] is False


def test_verify_flipped_byte(asset: Path):
    _prepare(asset)
    f = asset / "rebases/13/predictions.candidate.jsonl"
    b = bytearray(f.read_bytes())
    b[20] ^= 0x01
    f.write_bytes(bytes(b))
    r = runner.invoke(app, ["verify", str(asset / "rebases/13")])
    assert r.exit_code == 4, r.output
    assert "V-HASH-MISMATCH" in r.output and "predictions.candidate.jsonl" in r.output
    data = json.loads(runner.invoke(app, ["verify", str(asset / "rebases/13"), "--json"]).output)
    assert data["failed"] == "V-HASH-MISMATCH"


def _rehash(asset: Path, n: int, name: str) -> None:
    m = asset / f"rebases/{n}/MANIFEST.sha256"
    lines = []
    for line in m.read_text().splitlines():
        digest, rel = line.split("  ", 1)
        if rel == f"rebases/{n}/{name}":
            digest = sha256_file(asset / rel)
        lines.append(f"{digest}  {rel}")
    m.write_text("\n".join(lines) + "\n")


def test_verify_scores_tampered(asset: Path):
    _prepare(asset)
    f = asset / "rebases/14/scores.json"
    data = json.loads(f.read_text())
    data["slices"][0]["delta"] += 1e-5
    f.write_text(json.dumps(data, indent=2))
    _rehash(asset, 14, "scores.json")
    r = runner.invoke(app, ["verify", "rebases/14", "--asset", str(asset)])
    assert r.exit_code == 4 and "V-SCORES-MISMATCH" in r.output and "slices[0].delta" in r.output


def test_verify_schema_invalid(asset: Path):
    _prepare(asset)
    f = asset / "rebases/13/gate.json"
    data = json.loads(f.read_text())
    data["status"] = "maybe"
    f.write_text(json.dumps(data))
    _rehash(asset, 13, "gate.json")
    r = runner.invoke(app, ["verify", "rebases/13", "--asset", str(asset)])
    assert r.exit_code == 4 and "V-SCHEMA-INVALID" in r.output


def test_verify_seal_after_rebase(asset: Path):
    _prepare(asset)
    meta = asset / "rebases/13/rebase.yaml"
    meta.write_text(meta.read_text().replace("2026-11-01T10:00:00Z", "2026-11-01T08:00:00Z"))
    assert "08:00:00Z" in meta.read_text()
    _rehash(asset, 13, "rebase.yaml")
    r = runner.invoke(app, ["verify", "rebases/13", "--asset", str(asset)])
    assert r.exit_code == 4 and "V-SEAL-AFTER-REBASE" in r.output


def test_verify_no_manifest(asset: Path):
    r = runner.invoke(app, ["verify", "rebases/13", "--asset", str(asset)])
    assert r.exit_code == 4 and "V-MANIFEST-MISSING" in r.output


# ---------------------------------------------------------------- executor in the transcript

EXAMPLES = Path(__file__).resolve().parents[4] / "spec" / "examples"
RECEIPT_KEYID = "cb54bac00a078e10310f8766a331895c9c69a8e4350ea81af133f66ea84955b0"


@pytest.mark.parametrize("name,n", [("pass", 13), ("blocked", 14)])
def test_verify_names_the_executor(name: str, n: int):
    r = runner.invoke(app, ["verify", f"rebases/{n}", "--asset", str(EXAMPLES / name)])
    assert r.exit_code == 0, r.output
    lines = r.output.strip().splitlines()
    chain = next(i for i, x in enumerate(lines) if x.startswith("receipt chain OK"))
    assert lines[chain + 1] == f"receipt rebases/{n}/receipt.json: executor mock, signed by {RECEIPT_KEYID[:12]}"
    assert lines[chain + 2] == f"receipt versions/v12/receipt.json: executor mock, signed by {RECEIPT_KEYID[:12]}"
    assert "evidence manifest OK  (2 files; approvals only, no rendered evidence pack)" in lines
    assert lines[-2] == f"verified rebase #{n}"
    assert lines[-1] == "MOCK: no model was trained"
    data = json.loads(runner.invoke(app, ["verify", f"rebases/{n}", "--asset", str(EXAMPLES / name), "--json"]).output)
    assert data["mock"] is True
    assert data["executors"] == [
        {"receipt": f"rebases/{n}/receipt.json", "executor": "mock", "keyid": RECEIPT_KEYID},
        {"receipt": "versions/v12/receipt.json", "executor": "mock", "keyid": RECEIPT_KEYID},
    ]


def test_verify_failure_still_ends_with_mock(tmp_path: Path):
    root = tmp_path / "pass"
    shutil.copytree(EXAMPLES / "pass", root)
    (root / "rebases/13/report.md").write_text("tampered\n")
    r = runner.invoke(app, ["verify", "rebases/13", "--asset", str(root)])
    assert r.exit_code == 4, r.output
    lines = r.output.strip().splitlines()
    assert lines[-2] == "verify failed: V-HASH-MISMATCH" and lines[-1] == "MOCK: no model was trained"
