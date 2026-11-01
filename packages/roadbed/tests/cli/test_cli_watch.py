from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cli_asset import _cache_dir, asset  # noqa: F401  (pytest fixtures)
from roadbed.cli import watch as watch_mod
from roadbed.cli.main import app

runner = CliRunner()
SHA = "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"


class FakeApi:
    calls: list = []
    fail = False

    def list_models(self, **kw):
        FakeApi.calls.append(kw)
        if FakeApi.fail:
            raise ConnectionError("no network")
        return [
            SimpleNamespace(id="Qwen/Qwen3Guard-Stream-4B", last_modified=datetime(2026, 9, 27, tzinfo=UTC)),
            SimpleNamespace(id="Qwen/Qwen3.8-27B", last_modified=datetime(2026, 8, 14, tzinfo=UTC)),
            SimpleNamespace(id="Qwen/Qwen3.8-27B-FP8", last_modified=datetime(2026, 8, 13, tzinfo=UTC)),
            SimpleNamespace(id="Qwen/Qwen3.6-27B", last_modified=datetime(2026, 4, 1, tzinfo=UTC)),
        ]

    def model_info(self, repo_id, **kw):
        return SimpleNamespace(id=repo_id, sha=SHA, card_data={"license": "apache-2.0"},
                               tags=["license:apache-2.0"], last_modified=datetime(2026, 8, 14, tzinfo=UTC))


@pytest.fixture(autouse=True)
def fake_hub(monkeypatch):
    FakeApi.calls = []
    FakeApi.fail = False
    monkeypatch.setattr(watch_mod, "HfApi", FakeApi)


def test_watch_in_asset_repo(asset: Path, monkeypatch, _cache_dir: Path):
    monkeypatch.chdir(asset)
    r = runner.invoke(app, ["watch", "--base", "Qwen/Qwen3.*"])
    assert r.exit_code == 0, r.output
    assert r.output.strip() == "new base: Qwen/Qwen3.8-27B @1d4bf0f2ff60  license apache-2.0: allowed by policy"
    assert FakeApi.calls[0]["author"] == "Qwen" and FakeApi.calls[0]["search"] == "Qwen3"
    assert FakeApi.calls[0]["sort"] == "lastModified"
    cache = json.loads((_cache_dir / "watch.json").read_text())
    entry = cache["entries"]["Qwen/Qwen3.*"]
    assert entry["sha"] == SHA and entry["fetched_at"]


def test_watch_outside_asset_prints_license_only(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["watch", "--base", "Qwen/Qwen3.*-27B"])
    assert r.exit_code == 0, r.output
    assert r.output.strip() == "new base: Qwen/Qwen3.8-27B @1d4bf0f2ff60  license apache-2.0"


def test_watch_no_match_exits_2(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["watch", "--base", "Qwen/Qwen9.*"])
    assert r.exit_code == 2


def test_watch_offline_and_network_failure_use_cache(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["watch", "--base", "Qwen/Qwen3.*"]).exit_code == 0
    n = len(FakeApi.calls)
    r = runner.invoke(app, ["watch", "--base", "Qwen/Qwen3.*", "--offline"])
    assert r.exit_code == 0 and len(FakeApi.calls) == n
    assert "Qwen/Qwen3.8-27B @1d4bf0f2ff60" in r.output and "(cached 0s)" in r.output
    FakeApi.fail = True
    r = runner.invoke(app, ["watch", "--base", "Qwen/Qwen3.*", "--json"])
    data = json.loads(r.output)
    assert r.exit_code == 0 and data["source"] == "cache" and data["repo"] == "Qwen/Qwen3.8-27B"


def test_watch_offline_without_cache_fails(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["watch", "--base", "Qwen/Qwen3.*", "--offline"])
    assert r.exit_code == 1 and "no cached result" in r.output


def test_age():
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    assert watch_mod.age("2026-11-01T11:58:00Z", now) == "2m"
    assert watch_mod.age("2026-11-01T09:00:00Z", now) == "3h"
    assert watch_mod.age("2026-10-30T12:00:00Z", now) == "2d"
