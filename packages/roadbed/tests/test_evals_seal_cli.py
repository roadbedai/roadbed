"""roadbed evals seal: a local seal of the candidate that evals build wrote, with no control plane."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from test_evals_support import FIXTURE
from typer.testing import CliRunner

from roadbed.asset import ObjectStore, validate_file
from roadbed.asset.files import tree_files
from roadbed.cli.main import app

cli = CliRunner()
AS_OF = "2026-11-02T09:00:00Z"
HID = "H-2026-11-02"


def scaffold(tmp_path: Path, *, synthetic: bool = True) -> Path:
    """roadbed new, then evals build with the candidate in tmp/vault and the store in tmp/objects."""
    root = tmp_path / "acme"
    flag = ["--synthetic"] if synthetic else []
    r = cli.invoke(app, ["new", str(root), "--model", "claims-triage", "--slices", str(FIXTURE / "slices.yaml"),
                         "--output-schema", str(FIXTURE / "output.schema.json"), *flag])
    assert r.exit_code == 0, r.output
    r = cli.invoke(app, ["evals", "build", "--asset", str(root), "--events", str(FIXTURE), "--as-of", AS_OF, "--seed", "7",
                         "--holdout-out", str(tmp_path / "vault"), "--objects", str(tmp_path / "objects")])
    assert r.exit_code == 0, r.output
    return root


def seal(tmp_path: Path, root: Path, *extra: str):
    return cli.invoke(app, ["evals", "seal", "--asset", str(root), "--candidate", str(tmp_path / "vault"), "--sealed-by", "vik",
                            "--objects", str(tmp_path / "objects"), *extra])


@pytest.fixture
def asset(tmp_path) -> Path:
    return scaffold(tmp_path)


def test_writes_a_local_seal_and_updates_roadbed_yaml(tmp_path, asset):
    r = seal(tmp_path, asset, "--json")
    assert r.exit_code == 0, r.output
    o = json.loads(r.output)
    d = asset / "data/holdout" / HID
    doc = json.loads((d / "SEAL.json").read_text())
    assert doc["provenance"] == "local" and "audit_seq" not in doc and doc["sealed_by"] == "vik"
    assert doc["snapshot"] == "S-2026-11-02" and doc["synthetic"] is True and o["seal"] == doc
    for rel in (f"data/holdout/{HID}/SEAL.json", f"data/holdout/{HID}/holdout.jsonl", "roadbed.yaml"):
        validate_file(asset, rel, strict=True)
    assert ObjectStore(tmp_path / "objects").has(doc["minhash_oid"]) and o["objects"] == [doc["minhash_oid"]]
    data = yaml.safe_load((asset / "roadbed.yaml").read_text())["data"]
    assert data["holdout"] == {"id": HID, "path": f"data/holdout/{HID}", "sealed_by": "vik", "sealed_at": doc["sealed_at"]}
    assert data["snapshot"] == "S-2026-11-02"


def test_text_output(tmp_path, asset):
    r = seal(tmp_path, asset)
    assert r.exit_code == 0, r.output
    assert "local seal, no audit seq" in r.output and "record the Merkle root and the seal commit outside" in r.output
    assert f'git add -A && git commit -m "seal {HID}"' in r.output and "SYNTHETIC" in r.output


def test_raw_inputs_never_enter_the_asset(tmp_path, asset):
    assert seal(tmp_path, asset).exit_code == 0
    texts = [json.loads(x)["text"] for x in (tmp_path / "vault/inputs.jsonl").read_text().splitlines()]
    narrative = json.loads(texts[0])["narrative"]
    assert len(narrative) > 20
    for rel in tree_files(asset):
        assert narrative not in (asset / rel).read_text(encoding="utf-8", errors="replace"), rel


def test_a_second_seal_of_the_same_id_is_refused(tmp_path, asset):
    assert seal(tmp_path, asset).exit_code == 0
    before = (asset / "data/holdout" / HID / "SEAL.json").read_bytes()
    r = seal(tmp_path, asset)
    assert r.exit_code == 2 and "written once" in r.output
    assert (asset / "data/holdout" / HID / "SEAL.json").read_bytes() == before


def test_objects_inside_the_asset_is_refused(tmp_path, asset):
    r = cli.invoke(app, ["evals", "seal", "--asset", str(asset), "--candidate", str(tmp_path / "vault"), "--sealed-by", "vik",
                         "--objects", str(asset / "objects")])
    assert r.exit_code == 2 and "outside the asset" in r.output
    assert not (asset / "data/holdout").exists() and not (asset / "objects").exists()


def test_a_flipped_candidate_row_is_refused(tmp_path, asset):
    f = tmp_path / "vault/candidate.jsonl"
    lines = f.read_text().splitlines()
    row = json.loads(lines[0])
    row["expected"] = {**row["expected"], "severity": 5 if row["expected"]["severity"] != 5 else 1}
    row["row_hash"] = "0" * 64
    f.write_text("\n".join([json.dumps(row), *lines[1:]]) + "\n")
    r = seal(tmp_path, asset)
    assert r.exit_code == 2 and "Merkle root" in r.output
    assert not (asset / "data/holdout").exists()


def test_a_real_candidate_into_a_synthetic_asset_is_refused(tmp_path, asset):
    meta = tmp_path / "vault/candidate.json"
    meta.write_text(json.dumps({**json.loads(meta.read_text()), "synthetic": False}))
    r = seal(tmp_path, asset)
    assert r.exit_code == 2 and "synthetic" in r.output
    assert not (asset / "data/holdout").exists()


def test_sealed_by_must_be_a_user_id(tmp_path, asset):
    r = cli.invoke(app, ["evals", "seal", "--asset", str(asset), "--candidate", str(tmp_path / "vault"), "--sealed-by",
                         "vik@acme.example", "--objects", str(tmp_path / "objects")])
    assert r.exit_code == 2 and "sealed_by" in r.output
    r = cli.invoke(app, ["evals", "seal", "--asset", str(asset), "--candidate", str(tmp_path / "vault"),
                         "--objects", str(tmp_path / "objects")])
    assert r.exit_code == 2  # --sealed-by is required: a seal names a person


def test_the_next_build_reads_the_local_seal(tmp_path, asset):
    from roadbed.cli.evals import read_sealed

    assert seal(tmp_path, asset).exit_code == 0
    cfg = yaml.safe_load((asset / "roadbed.yaml").read_text())
    rows, synthetic = read_sealed(asset, cfg["data"]["holdout"]["path"])
    assert len(rows) == json.loads((asset / "data/holdout" / HID / "SEAL.json").read_text())["rows"] > 0 and synthetic is True
