"""`roadbed watch`: find the newest Hugging Face Hub repo that matches a base glob."""

from __future__ import annotations

import fnmatch
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from huggingface_hub import HfApi

LIST_LIMIT = 500


class NoMatch(Exception):
    pass


class NoCache(Exception):
    pass


def cache_path() -> Path:
    base = os.environ.get("ROADBED_CACHE_DIR")
    return (Path(base) if base else Path.home() / ".cache" / "roadbed") / "watch.json"


def _split(glob: str) -> tuple[str | None, str]:
    """Author and a plain search prefix from a glob such as Qwen/Qwen3.*"""
    author, _, name = glob.partition("/")
    if not name:
        return None, _prefix(author)
    return _prefix(author) or None, _prefix(name)


def _prefix(s: str) -> str:
    out = []
    for ch in s:
        if ch in "*?[":
            break
        out.append(ch)
    # Hub search is a substring match; drop a trailing "." so "Qwen3." searches "Qwen3".
    return "".join(out).rstrip(".-_")


def _license(info: Any) -> str | None:
    card = getattr(info, "card_data", None) or getattr(info, "cardData", None)
    if card is not None:
        lic = card.get("license") if hasattr(card, "get") else getattr(card, "license", None)
        if isinstance(lic, list):
            lic = lic[0] if lic else None
        if lic:
            return str(lic).lower()
    for tag in getattr(info, "tags", None) or []:
        if tag.startswith("license:"):
            return tag.split(":", 1)[1].lower()
    return None


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(dt)


def fetch(glob: str, api: Any | None = None) -> dict[str, Any]:
    """Query the Hub. Raises NoMatch when no repo id matches the glob."""
    api = api or HfApi()
    author, search = _split(glob)
    models = list(api.list_models(author=author, search=search or None, sort="lastModified", limit=LIST_LIMIT))
    matches = [m for m in models if fnmatch.fnmatchcase(m.id, glob)]
    if not matches:
        raise NoMatch(glob)
    # Newest first. Fall back to list order (the Hub sorts by lastModified, newest first).
    newest = max(
        enumerate(matches),
        key=lambda im: (_iso(getattr(im[1], "last_modified", None)) or "", -im[0]),
    )[1]
    info = api.model_info(newest.id)
    return {
        "glob": glob,
        "repo": newest.id,
        "sha": getattr(info, "sha", None),
        "license": _license(info),
        "last_modified": _iso(getattr(info, "last_modified", None) or getattr(newest, "last_modified", None)),
        "fetched_at": _iso(datetime.now(UTC)),
    }


def read_cache() -> dict[str, Any]:
    try:
        return json.loads(cache_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_cache(result: dict[str, Any]) -> None:
    data = read_cache()
    data.setdefault("entries", {})[result["glob"]] = result
    data["last"] = result["glob"]
    p = cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def cached(glob: str | None) -> dict[str, Any]:
    """Cached result for glob (or the last watched glob when glob is None). Raises NoCache."""
    data = read_cache()
    key = glob if glob is not None else data.get("last")
    entry = data.get("entries", {}).get(key) if key else None
    if not entry:
        raise NoCache(glob or "any base")
    return entry


def age(fetched_at: str, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    then = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    s = max(0, int((now - then).total_seconds()))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"
