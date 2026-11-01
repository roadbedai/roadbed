from __future__ import annotations

import hashlib

import jsonschema
import pytest

from roadbed import io
from roadbed.hashing import sha256_canonical, sha256_file
from roadbed.scoring import load_scorers, score_row

from conftest import EXAMPLE, SCORERS


def test_jsonl_round_trip(tmp_path):
    rows = [{"id": "a", "x": [1, 2]}, {"id": "b", "ü": "é"}]
    p = tmp_path / "sub" / "r.jsonl"
    io.write_jsonl(p, rows)
    p.write_text(p.read_text() + "\n")  # trailing blank line is ignored
    assert io.read_jsonl(p) == rows


def test_jsonl_rejects_non_object(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text("[1, 2]\n")
    with pytest.raises(ValueError):
        io.read_jsonl(p)


def test_yaml_round_trip_keeps_order(tmp_path):
    data = {"z": 1, "a": {"b": [1, 2]}, "s": "x"}
    p = tmp_path / "c.yaml"
    io.write_yaml(p, data)
    assert io.read_yaml(p) == data
    assert p.read_text().startswith("z:")


def test_load_asset_valid_and_invalid(tmp_path):
    data = io.load_asset(EXAMPLE)
    assert data["synthetic"] is True and data["data"]["holdout"]["sealed_at"] == "2026-11-01T09:02:00Z"
    bad = dict(io.read_yaml(EXAMPLE / "roadbed.yaml"))
    del bad["gate"]
    io.write_yaml(tmp_path / "roadbed.yaml", bad)
    with pytest.raises(jsonschema.ValidationError):
        io.load_asset(tmp_path)


def test_load_asset_accepts_unquoted_timestamp(tmp_path):
    text = (EXAMPLE / "roadbed.yaml").read_text().replace('"2026-11-01T09:02:00Z"', "2026-11-01T09:02:00Z")
    (tmp_path / "roadbed.yaml").write_text(text)
    assert io.load_asset(tmp_path)["data"]["holdout"]["sealed_at"] == "2026-11-01T09:02:00Z"


def test_sha256_file(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"abc")
    assert sha256_file(p) == hashlib.sha256(b"abc").hexdigest()


def test_sha256_canonical_ignores_key_order():
    assert sha256_canonical({"a": 1, "b": [1.5, "x"]}) == sha256_canonical({"b": [1.5, "x"], "a": 1})
    assert sha256_canonical({"a": 1}) == hashlib.sha256(b'{"a":1}').hexdigest()


def test_score_row():
    exp = {"route": "siu", "severity": 3}
    assert score_row(exp, exp, SCORERS) == 100.0
    assert score_row(exp, {"route": "siu", "severity": 4}, SCORERS) == pytest.approx(60.0)
    assert score_row(exp, {"route": "standard", "severity": 3}, SCORERS) == pytest.approx(40.0)
    assert score_row(exp, {}, SCORERS) == 0.0


def test_load_scorers():
    assert load_scorers(EXAMPLE / "evals/scorers.yaml").model_dump(exclude_none=True) == {
        **SCORERS.model_dump(exclude_none=True),
        "synthetic": True,
    }
