"""A tiny SYNTHETIC asset repo for CLI tests: 60 holdout rows, rebase 13 (pass) and 14 (blocked).

Only line=bodily_injury (30 rows) reaches min_n; every other slice is low-n.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from roadbed.io import write_jsonl, write_yaml

ROUTES = ["fast_track", "standard", "complex", "siu"]
STATES = ["CA", "TX", "FL"]
CHANNELS = ["broker", "direct", "partner"]
OTHER_LINES = ["auto_physical", "property", "liability", "workers_comp"]
REV = "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"


def _row(i: int) -> dict:
    rid = f"clm-{i:06d}"
    return {
        "id": rid,
        "slices": {
            "line": "bodily_injury" if i < 30 else OTHER_LINES[i % 4],
            "state": STATES[i % 3],  # 20 rows each: low-n
            "channel": CHANNELS[(i // 3) % 3],  # 20 rows each: low-n
        },
        "input_ref": "sha256:" + hashlib.sha256(rid.encode()).hexdigest(),
        "expected": {"route": ROUTES[i % 4], "severity": 1 + i % 5},
    }


def _wrong(exp: dict) -> dict:
    return {**exp, "route": ROUTES[(ROUTES.index(exp["route"]) + 1) % 4]}


def build_asset(root: Path) -> Path:
    write_yaml(root / "roadbed.yaml", {
        "spec": "roadbed/v0.1",
        "model": "claims-triage",
        "synthetic": True,
        "tenant": "Acme Mutual (SYNTHETIC)",
        "task": {"type": "structured-classification", "output_schema": "evals/output.schema.json"},
        "base": {"repo": "Qwen/Qwen3.6-27B", "revision": "unpinned"},
        "data": {"holdout": {"id": "H-TEST", "path": "data/holdout/H-TEST", "sealed_by": "vik",
                             "sealed_at": "2026-11-01T09:02:00Z"}},
        "evals": {"scorers": "evals/scorers.yaml", "slices": "evals/slices.yaml", "margin": -2.0, "min_n": 30},
        "recipe": "recipes/qwen3/lora-r16-sft@v4.yaml",
        "gate": "policy/gate.yaml",
        "alias": {"name": "claims-triage-prod", "version": "v12"},
    })
    (root / "SYNTHETIC.md").write_text("# SYNTHETIC\n", encoding="utf-8")
    write_yaml(root / "evals/scorers.yaml", {"type": "weighted_field_exact_match", "fields": [
        {"field": "route", "weight": 0.6}, {"field": "severity", "weight": 0.4}], "synthetic": True})
    write_yaml(root / "evals/slices.yaml", {"keys": {
        "line": ["auto_physical", "bodily_injury", "property", "liability", "workers_comp"],
        "state": STATES, "channel": CHANNELS}, "synthetic": True})
    write_yaml(root / "policy/gate.yaml", {
        "margin": -2.0, "min_n": 30, "ci": 0.95, "n_boot": 1000, "seed": 20261101,
        "block_on": ["slice_below_margin", "contamination", "license_denied"],
        "stages": {"shadow": {"stub": True}, "canary": {"stub": True}, "bias_tests": {"stub": True}},
        "synthetic": True})
    write_yaml(root / "policy/license.yaml", {"allow": ["apache-2.0"], "synthetic": True})

    hold = [_row(i) for i in range(60)]
    write_jsonl(root / "data/holdout/H-TEST/holdout.jsonl", hold)
    # incumbent: wrong route on every 6th row
    inc = [{"id": r["id"], "output": _wrong(r["expected"]) if i % 6 == 0 else dict(r["expected"])}
           for i, r in enumerate(hold)]
    # rebase 13: candidate is right everywhere -> better on every slice
    cand13 = [{"id": r["id"], "output": dict(r["expected"])} for r in hold]
    # rebase 14: candidate wrong route on bodily_injury rows 0..14 -> line=bodily_injury -20.0
    cand14 = [{"id": r["id"], "output": _wrong(r["expected"]) if i < 15 else dict(r["expected"])}
              for i, r in enumerate(hold)]
    for n, cand in ((13, cand13), (14, cand14)):
        d = root / "rebases" / str(n)
        write_jsonl(d / "predictions.incumbent.jsonl", inc)
        write_jsonl(d / "predictions.candidate.jsonl", cand)
        write_yaml(d / "rebase.yaml", {
            "synthetic": True, "rebase": n, "model": "claims-triage",
            "base": {"repo": "Qwen/Qwen3.8-27B", "revision": REV, "license": "apache-2.0"},
            "created_at": f"2026-11-01T1{n - 13}:00:00Z"})
    # two findings already logged by an earlier rebase -> rebase 14 logs F-0003
    for k in (1, 2):
        write_yaml(root / f"findings/F-{k:04d}.yaml", {"id": f"F-{k:04d}", "rebase": 5, "synthetic": True})
    return root


@pytest.fixture
def asset(tmp_path: Path) -> Path:
    return build_asset(tmp_path / "acme")


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "cache"
    monkeypatch.setenv("ROADBED_CACHE_DIR", str(d))
    return d
