"""`roadbed init` and ~/.config/roadbed/config.toml: what is written, what is refused, what the token file may be."""

from __future__ import annotations

import stat
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from roadbed.cli import config as config_mod
from roadbed.cli.main import app

cli = CliRunner()
KID = "a" * 64


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "cfg" / "roadbed" / "config.toml"
    monkeypatch.setenv("ROADBED_CONFIG", str(p))
    for v in ("ROADBED_URL", "ROADBED_TOKEN_FILE"):
        monkeypatch.delenv(v, raising=False)
    return p


def test_default_path_is_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("ROADBED_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_mod.config_path() == tmp_path / "roadbed" / "config.toml"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert config_mod.config_path() == Path.home() / ".config" / "roadbed" / "config.toml"


def test_init_writes_0600_toml(cfg_path: Path, tmp_path: Path):
    tok = tmp_path / "token"
    tok.write_text("t")
    r = cli.invoke(app, ["init", "--url", "https://cp.example/", "--token-file", str(tok), "--trust-key", KID])
    assert r.exit_code == 0, r.output
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600
    data = tomllib.loads(cfg_path.read_text())
    assert data == {"url": "https://cp.example", "token_file": str(tok), "trust_keys": [KID]}
    assert "t\n" not in cfg_path.read_text().splitlines()  # the token itself is never copied into the config
    c = config_mod.load()
    assert (c.url, c.token_file, c.trust_keys) == ("https://cp.example", str(tok), [KID])


def test_init_refuses_overwrite_without_force(cfg_path: Path):
    assert cli.invoke(app, ["init", "--url", "https://a.example"]).exit_code == 0
    r = cli.invoke(app, ["init", "--url", "https://b.example"])
    assert r.exit_code == 2 and "--force" in r.output
    assert cli.invoke(app, ["init", "--url", "https://b.example", "--force", "--json"]).exit_code == 0
    assert config_mod.load().url == "https://b.example"


@pytest.mark.parametrize("url", ["http://cp.example", "ftp://cp.example", "https://user:pw@cp.example", "https://cp.example/?x=1", "cp.example"])
def test_init_rejects_bad_urls(cfg_path: Path, url: str):
    r = cli.invoke(app, ["init", "--url", url])
    assert r.exit_code == 2 and not cfg_path.exists()


@pytest.mark.parametrize("url", ["http://localhost:8080", "http://127.0.0.1:8000", "http://[::1]:8000"])
def test_plain_http_only_for_loopback(cfg_path: Path, url: str):
    assert cli.invoke(app, ["init", "--url", url]).exit_code == 0


def test_init_rejects_bad_trust_key_and_missing_token_file(cfg_path: Path, tmp_path: Path):
    assert cli.invoke(app, ["init", "--url", "https://a.example", "--trust-key", "abc"]).exit_code == 2
    assert cli.invoke(app, ["init", "--url", "https://a.example", "--token-file", str(tmp_path / "nope")]).exit_code == 2
    assert not cfg_path.exists()


@pytest.mark.parametrize("text,msg", [
    ('url = "https://a.example"\ntoken = "secret"\n', "unknown keys token"),
    ('url = 5\n', "url must be a string"),
    ('trust_keys = ["xyz"]\n', "trust_keys"),
    ("url = \n", "config.toml"),
])
def test_bad_config_exits_2(cfg_path: Path, text: str, msg: str):
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(text)
    r = cli.invoke(app, ["status", "--remote", "--model", "m"])
    assert r.exit_code == 2 and msg in r.output
    assert "secret" not in r.output


def test_env_overrides(cfg_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text('url = "https://a.example"\n')
    monkeypatch.setenv("ROADBED_URL", "https://b.example")
    monkeypatch.setenv("ROADBED_TOKEN_FILE", str(tmp_path / "t"))
    c = config_mod.load()
    assert (c.url, c.token_file) == ("https://b.example", str(tmp_path / "t"))


def test_token_file_rules(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    tok = tmp_path / "t"
    tok.write_text("abc def\n")
    tok.chmod(0o600)
    with pytest.raises(config_mod.ConfigError, match="one token on one line"):
        config_mod.Config(url="https://a.example", token_file=str(tok)).token()
    tok.write_text("abc\n")
    tok.chmod(0o644)
    assert config_mod.Config(token_file=str(tok)).token() == "abc"
    assert "readable by other users" in capsys.readouterr().err
    with pytest.raises(config_mod.ConfigError, match="token file"):
        config_mod.Config(token_file=str(tmp_path / "missing")).token()
