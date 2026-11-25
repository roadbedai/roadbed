"""The sample's stand-in prediction script: plain Python, no roadbed import, no model."""

from __future__ import annotations

import json
import subprocess
import sys
from importlib import resources
from pathlib import Path

ROUTES = ["fast_track", "standard", "complex", "siu"]
SCRIPT = Path(str(resources.files("roadbed.samples").joinpath("acme-2k", "stand_in_predictions.py")))


def _holdout(path: Path, n: int) -> list[dict]:
    rows = [{"id": f"r{i:02d}", "expected": {"route": ROUTES[i % 4], "severity": 1 + i % 5}, "slices": {"line": "property"}} for i in range(n)]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return rows


def _read(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_stand_in_writes_both_prediction_files(tmp_path: Path) -> None:
    rows = _holdout(tmp_path / "holdout.jsonl", 13)
    out = tmp_path / "rebases" / "1"  # the script makes the directory
    run = subprocess.run([sys.executable, "-I", str(SCRIPT), str(tmp_path / "holdout.jsonl"), str(out)], capture_output=True, text=True, check=True)
    assert run.stdout.strip() == "stand-in predictions, no model was run"

    cand = _read(out / "predictions.candidate.jsonl")
    inc = _read(out / "predictions.incumbent.jsonl")
    assert cand == [{"id": r["id"], "output": r["expected"]} for r in rows]
    assert [r["id"] for r in inc] == [r["id"] for r in rows]
    wrong = [i for i, (a, r) in enumerate(zip(inc, rows)) if a["output"] != r["expected"]]
    assert wrong == [0, 6, 12]  # every sixth row
    for i in wrong:
        assert inc[i]["output"]["severity"] == rows[i]["expected"]["severity"]  # one field only
        assert inc[i]["output"]["route"] in ROUTES and inc[i]["output"]["route"] != rows[i]["expected"]["route"]
