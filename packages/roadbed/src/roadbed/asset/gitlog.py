"""Read-only git queries for the seal order (SPEC.md, Seal precedes training, leg 3).

Only plumbing that runs no repo-configured commands: log, rev-parse, merge-base, hash-object --no-filters, cat-file.
Hooks and fsmonitor are switched off on every call, and the system config is ignored.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_SAFE = ("-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", "-c", "core.autocrlf=false")


class GitError(Exception):
    pass


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GIT_LITERAL_PATHSPECS="1", LC_ALL="C")
    return env


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        r = subprocess.run(["git", *_SAFE, "-C", str(root), *args], capture_output=True, text=True, env=_env(), timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git {args[0]}: {e}") from None
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {r.stderr.strip() or r.returncode}")
    return r


def is_repo_root(root: Path) -> bool:
    """True when root is the top of its own git work tree (a clone of the asset), not a folder inside another repo."""
    try:
        r = git(root, "rev-parse", "--show-toplevel", check=False)
    except GitError:
        return False
    return r.returncode == 0 and Path(r.stdout.strip()).resolve() == root.resolve()


def ever_tracked(root: Path, *paths: str) -> bool:
    """True when any of paths appears in the history of HEAD."""
    r = git(root, "log", "--format=%H", "-1", "--", *paths, check=False)
    return r.returncode == 0 and bool(r.stdout.strip())


def commits_touching(root: Path, path: str) -> list[str]:
    return git(root, "log", "--format=%H", "--", path).stdout.split()


def added_in(root: Path, path: str) -> str:
    """The one commit that added path and never touched it again (a seal and a receipt are written once)."""
    touching = commits_touching(root, path)
    added = git(root, "log", "--diff-filter=A", "--format=%H", "--", path).stdout.split()
    if not added:
        raise GitError(f"{path} is not in the git history")
    if len(added) != 1 or touching != added:
        raise GitError(f"{path} was changed or re-added after it was first committed ({len(touching)} commits)")
    return added[0]


def matches_head(root: Path, path: str) -> bool:
    """True when the working file has the same bytes as HEAD:<path>."""
    head = git(root, "rev-parse", "--verify", "-q", f"HEAD:{path}", check=False)
    if head.returncode != 0:
        return False
    work = git(root, "hash-object", "--no-filters", "--", path, check=False)
    return work.returncode == 0 and work.stdout.strip() == head.stdout.strip()


def blob(root: Path, rev: str, path: str) -> bytes | None:
    """The raw bytes of path at rev (no filters), or None when rev does not have it."""
    try:
        r = subprocess.run(["git", *_SAFE, "-C", str(root), "cat-file", "blob", f"{rev}:{path}"], capture_output=True,
                           env=_env(), timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git cat-file: {e}") from None
    return r.stdout if r.returncode == 0 else None


def is_ancestor(root: Path, a: str, b: str) -> bool:
    r = git(root, "merge-base", "--is-ancestor", a, b, check=False)
    if r.returncode not in (0, 1):
        raise GitError(f"git merge-base: {r.stderr.strip()}")
    return r.returncode == 0
