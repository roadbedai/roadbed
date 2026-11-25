"""gitlog runs no repo-configured program: a hostile .git/config cannot run a script through git log."""

from __future__ import annotations

from pathlib import Path

from roadbed.asset import gitlog
from roadbed.asset.verify import _control_plane_file
from test_asset_support import git

FAKESIGN = ("#!/bin/sh\ncat >/dev/null\nprintf '[GNUPG:] SIG_CREATED \\n' >&2\n"
            "printf -- '-----BEGIN PGP SIGNATURE-----\\n\\nAAAA\\n-----END PGP SIGNATURE-----\\n'\n")


def _hostile_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repo with one signed commit whose config sets log.showSignature and a gpg.program that writes a marker."""
    root, marker = tmp_path / "acme", tmp_path / "PWNED"
    root.mkdir()
    sign, evil = tmp_path / "fakesign.sh", tmp_path / "evil.sh"
    sign.write_text(FAKESIGN)
    evil.write_text(f"#!/bin/sh\necho pwned >> {marker}\ncat >/dev/null\nexit 1\n")
    sign.chmod(0o755)
    evil.chmod(0o755)
    git(root, "init", "-q")
    (root / "a.txt").write_text("a\n")
    git(root, "add", "a.txt")
    git(root, "-c", f"gpg.program={sign}", "commit", "-q", "-S", "-m", "signed")
    git(root, "config", "log.showSignature", "true")
    git(root, "config", "gpg.program", str(evil))
    git(root, "log", "-1")  # the attack works through a plain git log
    assert marker.exists()
    marker.unlink()
    return root, marker


def test_git_log_never_runs_the_configured_gpg_program(tmp_path):
    root, marker = _hostile_repo(tmp_path)
    assert gitlog.git(root, "log", "--format=%H").stdout.strip()
    assert not marker.exists()


def test_the_control_plane_history_check_never_runs_the_configured_gpg_program(tmp_path):
    root, marker = _hostile_repo(tmp_path)
    (root / "rebases/1").mkdir(parents=True)
    assert _control_plane_file(root, root / "rebases/1") is None
    assert not marker.exists()
