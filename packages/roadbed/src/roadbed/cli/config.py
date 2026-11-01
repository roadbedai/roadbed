"""CLI config: ~/.config/roadbed/config.toml (or $XDG_CONFIG_HOME/roadbed, or $ROADBED_CONFIG).

    url = "https://cp.roadbed.example"           # control plane
    token_file = "~/.config/roadbed/token"       # API token, one line; never stored in this file
    trust_keys = ["cb54bac0..."]                 # key ids `roadbed verify` and `evidence verify` accept

Environment overrides: ROADBED_URL, ROADBED_TOKEN_FILE. The token is read only when a remote command runs and is
never printed, logged or written anywhere else.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

KEYID = re.compile(r"^[0-9a-f]{64}$")
KEYS = ("url", "token_file", "trust_keys")


class ConfigError(Exception):
    """Config missing or not valid. Exit 2."""


def config_path() -> Path:
    if p := os.environ.get("ROADBED_CONFIG"):
        return Path(p).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / "roadbed" / "config.toml"


def check_url(url: str) -> str:
    """https only, except loopback (a local control plane). No credentials, query or fragment in the URL."""
    u = urlsplit(url)
    if u.scheme not in ("https", "http") or not u.hostname:
        raise ConfigError(f"url must be https://host[:port][/path] (got {url!r})")
    if u.username or u.password or u.query or u.fragment:
        raise ConfigError("url must not hold credentials, a query or a fragment")
    if u.scheme == "http":
        host = u.hostname
        try:
            loop = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loop = host == "localhost"
        if not loop:
            raise ConfigError(f"plain http is allowed for localhost only (got {url!r}); use https")
    return url.rstrip("/")


@dataclass
class Config:
    url: str | None = None
    token_file: str | None = None
    trust_keys: list[str] = field(default_factory=list)
    path: Path | None = None

    def require_url(self) -> str:
        if not self.url:
            raise ConfigError(f"no control plane url: run `roadbed init --url URL` (config {config_path()}) or set ROADBED_URL")
        return check_url(self.url)

    def token(self) -> str | None:
        if not self.token_file:
            return None
        p = Path(self.token_file).expanduser()
        try:
            mode = p.stat().st_mode
            text = p.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise ConfigError(f"token file {p}: {e.strerror}") from None
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            print(f"warning: token file {p} is readable by other users; chmod 600 it", file=sys.stderr)
        if not text or any(c.isspace() for c in text):
            raise ConfigError(f"token file {p} must hold one token on one line")
        return text


def load() -> Config:
    p = config_path()
    data: dict[str, object] = {}
    if p.is_file():
        try:
            data = tomllib.loads(p.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            raise ConfigError(f"{p}: {e}") from None
        unknown = sorted(set(data) - set(KEYS))
        if unknown:
            raise ConfigError(f"{p}: unknown keys {', '.join(unknown)} (allowed: {', '.join(KEYS)})")
    cfg = Config(path=p if p.is_file() else None)
    url, tf, keys = data.get("url"), data.get("token_file"), data.get("trust_keys", [])
    if url is not None and not isinstance(url, str):
        raise ConfigError(f"{p}: url must be a string")
    if tf is not None and not isinstance(tf, str):
        raise ConfigError(f"{p}: token_file must be a string")
    if not isinstance(keys, list) or not all(isinstance(k, str) and KEYID.fullmatch(k) for k in keys):
        raise ConfigError(f"{p}: trust_keys must be a list of 64-hex key ids")
    cfg.url = os.environ.get("ROADBED_URL") or url
    cfg.token_file = os.environ.get("ROADBED_TOKEN_FILE") or tf
    cfg.trust_keys = list(keys)
    if cfg.url:
        cfg.url = check_url(cfg.url)
    return cfg


def render(url: str, token_file: str | None, trust_keys: list[str]) -> str:
    """TOML text. json.dumps of a str is a valid TOML basic string."""
    lines = ["# roadbed CLI config (roadbed init). The token itself lives in token_file, never here.",
             f"url = {json.dumps(check_url(url))}"]
    if token_file:
        lines.append(f"token_file = {json.dumps(token_file)}")
    for k in trust_keys:
        if not KEYID.fullmatch(k):
            raise ConfigError(f"trust key must be a 64-hex key id (got {k!r})")
    lines.append("trust_keys = [" + ", ".join(json.dumps(k) for k in trust_keys) + "]")
    return "\n".join(lines) + "\n"


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_suffix(".toml.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)
