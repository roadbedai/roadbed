from __future__ import annotations

import json
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
