""".github/scripts/dco.sh, the CI DCO check: fed fake commit objects in the shape of the GitHub pull request commits API.
Maintainers (users in CODEOWNERS) and bots are exempt; every other commit needs a Signed-off-by line."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".github" / "scripts" / "dco.sh"
pytestmark = pytest.mark.skipif(shutil.which("jq") is None or shutil.which("bash") is None, reason="needs bash and jq")


def commit(sha: str, login: str | None, message: str, *, email: str = "a@example.com", parents: int = 1) -> dict:
    return {"sha": sha * 40, "author": {"login": login} if login else None,
            "commit": {"message": message, "author": {"name": "A", "email": email}}, "parents": [{"sha": "0" * 40}] * parents}


def run(tmp_path: Path, commits: list[dict], owners: str = "/spec/      @edwinsamar\n/docs/ @roadbedai/docs\n"):
    (tmp_path / "CODEOWNERS").write_text("# owners\n" + owners)
    stream = "".join(json.dumps(c) + "\n" for c in commits)
    return subprocess.run(["bash", str(SCRIPT), str(tmp_path / "CODEOWNERS")], input=stream, capture_output=True, text=True, timeout=30)


EXEMPT = [
    commit("a", "edwinsamar", "Fix a thing"),
    commit("b", "EdwinSamar", "Fix a thing"),  # logins are case-insensitive
    commit("c", "dependabot[bot]", "Bump x"),
    commit("d", None, "Merge branch main", parents=2),
    commit("e", "someone", "Fix a thing\n\nSigned-off-by: Some One <some@example.com>\n"),
]


def test_maintainer_bot_merge_and_signed_commits_pass(tmp_path):
    r = run(tmp_path, EXEMPT)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == ""


def test_an_unsigned_commit_from_a_contributor_fails_and_is_named(tmp_path):
    r = run(tmp_path, [*EXEMPT, commit("f", "someone", "Fix a thing"), commit("9", None, "No linked account")])
    assert r.returncode == 1
    assert "commit ffffffffffff (author someone) has no Signed-off-by line" in r.stdout
    assert "commit 999999999999 (author unknown) has no Signed-off-by line" in r.stdout
    assert "aaaaaaaaaaaa" not in r.stdout and "cccccccccccc" not in r.stdout


def test_a_team_owner_or_a_sign_off_without_an_email_does_not_exempt(tmp_path):
    r = run(tmp_path, [commit("f", "roadbedai", "x"), commit("8", "someone", "x\n\nSigned-off-by: someone\n")])
    assert r.returncode == 1 and "ffffffffffff" in r.stdout and "888888888888" in r.stdout


def test_an_owner_listed_by_email_is_exempt(tmp_path):
    r = run(tmp_path, [commit("f", None, "x", email="Maint@Example.com")], owners="/spec/ maint@example.com\n")
    assert r.returncode == 0, r.stdout
