from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from roadbed.contracts import FieldWeight, GatePolicy, Scorers, Stage, Stages

SPEC_DIR = Path(__file__).resolve().parents[3] / "spec"
EXAMPLE = SPEC_DIR / "examples" / "claims-triage"

ROUTES = ["fast_track", "standard", "complex", "siu"]


def make_policy(**kw) -> GatePolicy:
    base = dict(
        margin=-2.0,
        min_n=30,
        ci=0.95,
        n_boot=2000,
        seed=20261101,
        block_on=["slice_below_margin", "contamination", "license_denied"],
        stages=Stages(shadow=Stage(stub=True), canary=Stage(stub=True), bias_tests=Stage(stub=True)),
    )
    base.update(kw)
    return GatePolicy(**base)


SCORERS = Scorers(
    type="weighted_field_exact_match",
    fields=[FieldWeight(field="route", weight=0.6), FieldWeight(field="severity", weight=0.4)],
)


def holdout_row(i: int, **slices: str) -> dict:
    rid = f"clm-{i:06d}"
    return {
        "id": rid,
        "slices": slices,
        "input_ref": "sha256:" + hashlib.sha256(rid.encode()).hexdigest(),
        "expected": {"route": ROUTES[i % 4], "severity": 1 + i % 5},
    }


def wrong_route(expected: dict) -> dict:
    """Output with the route wrong and severity right: row score 40."""
    return {**expected, "route": ROUTES[(ROUTES.index(expected["route"]) + 1) % 4]}


def pred(row: dict, output: dict | None = None) -> dict:
    return {"id": row["id"], "output": output if output is not None else dict(row["expected"])}


@pytest.fixture
def policy() -> GatePolicy:
    return make_policy()
