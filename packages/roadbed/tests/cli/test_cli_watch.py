"""`roadbed watch` end to end against the recorded Hub cassettes (tests/cassettes)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli_asset import _cache_dir, asset  # noqa: F401  (pytest fixtures)
from roadbed.cli import watch as watch_mod
from roadbed.cli.main import app
from roadbed.io import write_yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "watch"))  # shared cassette helper
from hub_vcr import cassette  # noqa: E402

runner = CliRunner()
SHA = "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"
GLOB = "Qwen/Qwen3.*-27B"


@pytest.fixture(autouse=True)
def hub():
    with cassette("hub_scan_qwen3_27b.yaml"):
        yield


def test_watch_in_asset_repo_reports_once(asset: Path, monkeypatch, _cache_dir: Path):
    monkeypatch.chdir(asset)
    r = runner.invoke(app, ["watch", "--base", GLOB])
    assert r.exit_code == 0, r.output
    assert r.output.strip() == "new base: Qwen/Qwen3.8-27B @1d4bf0f2ff60  license apache-2.0: allowed by policy"
    entry = json.loads((_cache_dir / "watch.json").read_text())["entries"][GLOB]
    assert entry["sha"] == SHA and entry["decision"] == "allow"
    assert watch_mod.cached(None)["repo"] == "Qwen/Qwen3.8-27B"
    r = runner.invoke(app, ["watch", "--base", GLOB])
    assert r.exit_code == 0 and r.output.strip() == f"no new base for {GLOB}"


def test_watch_review_and_json(asset: Path, monkeypatch):
    monkeypatch.chdir(asset)
    write_yaml(asset / "policy/license.yaml", {"allow": ["mit"], "review": ["apache*"], "synthetic": True})
    r = runner.invoke(app, ["watch", "--base", GLOB, "--json"])
    data = json.loads(r.output)
    assert r.exit_code == 0 and data["ok"] is True
    [d] = data["detections"]
    assert (d["repo"], d["revision"], d["license"], d["license_source"], d["source"]) == (
        "Qwen/Qwen3.8-27B", SHA, "apache-2.0", "cardData", "hub")
    assert d["decision"]["decision"] == "review"


def test_watch_bad_policy_fails(asset: Path, monkeypatch):
    monkeypatch.chdir(asset)
    (asset / "policy/license.yaml").write_text("allow: [apache-2.0]\nallow_everything: true\n")
    r = runner.invoke(app, ["watch", "--base", GLOB])
    assert r.exit_code == 2 and "policy/license.yaml" in r.output


def test_watch_outside_asset_prints_license_only(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["watch", "--base", GLOB])
    assert r.exit_code == 0, r.output
    assert r.output.strip() == "new base: Qwen/Qwen3.8-27B @1d4bf0f2ff60  license apache-2.0"


def test_watch_no_match_exits_2(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["watch", "--base", "Qwen/Qwen3.*-99B"])
    assert r.exit_code == 2


def test_watch_bad_glob_exits_2(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["watch", "--base", "Qwen/../x"])
    assert r.exit_code == 2 and "base glob" in r.output


def test_watch_offline_uses_cache(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["watch", "--base", GLOB]).exit_code == 0
    r = runner.invoke(app, ["watch", "--base", GLOB, "--offline", "--state", str(tmp_path / "s2.json")])
    assert r.exit_code == 0, r.output
    assert "Qwen/Qwen3.8-27B @1d4bf0f2ff60" in r.output and "(cached " in r.output


def test_watch_offline_without_cache_fails(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["watch", "--base", GLOB, "--offline"])
    assert r.exit_code == 3 and "no cached Hub listing" in r.output  # Hub unreachable is a remote error


def test_age():
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    assert watch_mod.age("2026-11-01T11:58:00Z", now) == "2m"
    assert watch_mod.age("2026-11-01T09:00:00Z", now) == "3h"
    assert watch_mod.age("2026-10-30T12:00:00Z", now) == "2d"


def test_cached_rejects_malformed_entry(_cache_dir: Path):
    _cache_dir.mkdir(parents=True)
    (_cache_dir / "watch.json").write_text(json.dumps({"last": "g", "entries": {"g": {"repo": "../x", "sha": "a" * 40}}}))
    with pytest.raises(watch_mod.NoCache):
        watch_mod.cached(None)
