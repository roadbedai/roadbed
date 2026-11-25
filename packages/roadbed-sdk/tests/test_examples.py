"""The SDK examples in examples/sdk run as their docstrings say, with no control plane."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import TEST_KEY

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "sdk"


def test_reconcile_outcomes_writes_local_outcomes(tmp_path: Path) -> None:
    ids = ["ix_aaaaaaaaaaaaaaaaaaaaaaaaaa", "ix_bbbbbbbbbbbbbbbbbbbbbbbbbb"]
    (tmp_path / "outcomes.csv").write_text("interaction_id,observed_at,paid\n" + "".join(f"{i},2026-11-20T10:00:00Z,true\n" for i in ids))
    env = {**os.environ, "ROADBED_TENANT_KEY": TEST_KEY.decode(), "HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, str(EXAMPLES / "reconcile_outcomes.py"), "outcomes.csv"], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "rows: 2, skipped: 0, error: None" in r.stdout, r.stdout
    rows = [json.loads(x) for x in (tmp_path / "roadbed-events" / "outcomes.jsonl").read_text().splitlines()]
    assert [o["interaction_id"] for o in rows] == ids and all(o["outcome"] == {"paid": True} for o in rows)
