"""CLI glue for `roadbed watch`: file locations, the last detection that `roadbed rebase` reads, and age text.

The Hub polling, state and 24 h cache live in roadbed.watch; the license policy in roadbed.license_policy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from roadbed.watch import SHA, Detection, default_cache_dir, valid_repo, write_json_atomic


class NoCache(Exception):
    pass


def cache_path() -> Path:
    """The last detection per glob, for `roadbed rebase` without --base."""
    return default_cache_dir() / "watch.json"


def state_path() -> Path:
    """Default state file: which bases were already detected."""
    return default_cache_dir() / "watch-state.json"


def read_cache() -> dict[str, Any]:
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_last(d: Detection) -> None:
    data = read_cache()
    old = data.get("entries")
    entries: dict[str, Any] = old if isinstance(old, dict) else {}
    entries[d.glob] = {
        "glob": d.glob,
        "repo": d.repo,
        "sha": d.revision,
        "license": d.license,
        "decision": d.decision.decision if d.decision else None,
        "fetched_at": d.fetched_at,
    }
    write_json_atomic(cache_path(), {"entries": entries, "last": d.glob})


def cached(glob: str | None) -> dict[str, Any]:
    """Last detection for glob (or the last watched glob when None). Raises NoCache, also for a malformed entry."""
    data = read_cache()
    key = glob if glob is not None else data.get("last")
    all_entries = data.get("entries")
    entries: dict[str, Any] = all_entries if isinstance(all_entries, dict) else {}
    entry = entries.get(key) if isinstance(key, str) else None
    if (not isinstance(entry, dict) or not valid_repo(entry.get("repo"))
            or not isinstance(entry.get("sha"), str) or not SHA.fullmatch(entry["sha"])):
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
