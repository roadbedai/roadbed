"""The README quickstart, run as a user runs it: every command from the fenced block under the Quickstart heading,
in order, in a fresh directory with a real git repository, through the `roadbed` console script of this environment
(roadbed.cli.main has no __main__ module). The test reads README.md, so the test and the README cannot drift."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

README = Path(__file__).resolve().parents[3] / "README.md"
MAX_SECONDS = 120


def quickstart_commands() -> list[str]:
    text = README.read_text(encoding="utf-8")
    m = re.search(r"^## [^\n]*[Qq]uickstart[^\n]*\n.*?^```[^\n]*\n(.*?)^```", text, re.S | re.M)
    assert m, "README.md has no fenced block under a Quickstart heading"
    lines = [ln.strip() for ln in m.group(1).splitlines()]
    return [ln.replace("<you>", "vik") for ln in lines if ln and not ln.startswith("#") and not ln.startswith("pip install")]


def test_quickstart(tmp_path, record_property):
    bindir = Path(sys.executable).parent
    assert (bindir / "roadbed").is_file(), f"no roadbed console script next to {sys.executable}"
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Quickstart Test\n\temail = quickstart@example.com\n")
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "GIT_CONFIG_GLOBAL": str(gitconfig),
           "GIT_CONFIG_SYSTEM": os.devnull, "NO_COLOR": "1", "COLUMNS": "200"}
    work = tmp_path / "work"
    work.mkdir()
    commands = quickstart_commands()
    assert len(commands) >= 10, commands
    cwd, outputs, elapsed = work, [], 0.0
    for cmd in commands:
        cwdfile = tmp_path / "cwd"
        t0 = time.perf_counter()
        r = subprocess.run(["bash", "-c", f'set -e\n{cmd}\npwd -P > "{cwdfile}"'], cwd=cwd, env=env,
                           capture_output=True, text=True, timeout=MAX_SECONDS)
        elapsed += time.perf_counter() - t0
        assert r.returncode == 0, f"{cmd}\n{r.stdout}\n{r.stderr}"
        cwd = Path(cwdfile.read_text().strip())
        outputs.append((cmd, r.stdout + r.stderr))
    verifies = [out for cmd, out in outputs if re.search(r"\broadbed verify\b", cmd)]
    assert len(verifies) == 2
    assert "verified seal" in verifies[0] and "verified rebase" in verifies[1], verifies
    record_property("quickstart_seconds", round(elapsed, 1))
    print(f"\nquickstart: {len(commands)} commands, {elapsed:.1f} s wall time (install excluded)")
    assert elapsed < MAX_SECONDS
