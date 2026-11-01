"""Pydantic v2 models for every file in the Roadbed v0.1 asset format.

The JSON Schemas in spec/schemas/v0.1 are generated from these models by
`export_schemas`. A test checks that the files on disk match the models.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

SPEC = "roadbed/v0.1"
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_BASE_ID = "https://github.com/roadbedai/roadbed/blob/main/spec/schemas/v0.1/"

Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", description="Lowercase hex SHA-256 digest.")]
SpecVersion = Annotated[Literal["roadbed/v0.1"], Field(description="Spec version of this file.")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- roadbed.yaml


class Task(_Strict):
    type: Literal["structured-classification"] = Field(description="Kind of task the model does.")
    output_schema: str = Field(description="Path (from asset root) to the JSON Schema of one model output.")


class Base(_Strict):
    repo: str = Field(description="Hugging Face repo id of the open-weight base model, e.g. Qwen/Qwen3.6-27B.")
    revision: str = Field(
        pattern=r"^([0-9a-f]{40}|unpinned)$",
        description="40-hex commit of the base repo, or 'unpinned'.",
    )


class HoldoutRef(_Strict):
    id: str = Field(description="Holdout id, e.g. H-2026-11-01.")
    path: str = Field(description="Directory (from asset root) that holds holdout.jsonl.")
    sealed_by: str = Field(description="Person who sealed the holdout.")
    sealed_at: AwareDatetime = Field(description="When the holdout was sealed (RFC 3339, with zone).")


class Data(_Strict):
    holdout: HoldoutRef


class Evals(_Strict):
    scorers: str = Field(description="Path to scorers.yaml.")
    slices: str = Field(description="Path to slices.yaml.")
    margin: float = Field(description="Most negative allowed slice delta, in score points.")
    min_n: int = Field(ge=1, description="Slices with fewer rows are flagged low_n and never block.")


class Alias(_Strict):
    name: str = Field(description="Serving alias the gate protects.")
    version: str = Field(description="Current version behind the alias.")


class RoadbedConfig(_Strict):
    """roadbed.yaml at the asset repo root."""

    model_config = ConfigDict(extra="forbid", title="roadbed.yaml")

    spec: SpecVersion
    model: str = Field(description="Name of the fine-tuned model this asset repo governs.")
    synthetic: bool = Field(description="True when the asset repo holds illustrative, synthetic data.")
    tenant: str = Field(description="Display name of the tenant.")
    task: Task
    base: Base
    data: Data
    evals: Evals
    recipe: str = Field(description="Path or ref of the training recipe (may be a stub).")
    gate: str = Field(description="Path to the gate policy, e.g. policy/gate.yaml.")
    alias: Alias


# ---------------------------------------------------------------- rows


class HoldoutRow(_Strict):
    """One line of data/holdout/<id>/holdout.jsonl."""

    model_config = ConfigDict(extra="forbid", title="Holdout row")

    id: str = Field(min_length=1, description="Stable row id, e.g. clm-000001.")
    slices: dict[str, str] = Field(description="Slice key to slice value, e.g. {line: bodily_injury}.")
    input_ref: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$",
        description="Content address of the input; the raw input is not stored in the holdout.",
    )
    expected: dict[str, Any] = Field(description="Reviewer verdict. Must validate against task.output_schema.")


class PredictionRow(_Strict):
    """One line of rebases/<n>/predictions.{incumbent,candidate}.jsonl."""

    model_config = ConfigDict(extra="forbid", title="Prediction row")

    id: str = Field(min_length=1, description="Holdout row id this prediction answers.")
    output: dict[str, Any] = Field(description="Model output. Must validate against task.output_schema.")


# ---------------------------------------------------------------- evals/scorers.yaml


class FieldWeight(_Strict):
    field: str = Field(description="Top-level output field compared by exact match.")
    weight: float = Field(gt=0, description="Relative weight of this field.")


class Scorers(_Strict):
    """evals/scorers.yaml. Row score = 100 * sum(weight * match) / sum(weight)."""

    model_config = ConfigDict(extra="forbid", title="scorers.yaml")

    type: Literal["weighted_field_exact_match"] = Field(description="Scorer kind.")
    fields: list[FieldWeight] = Field(min_length=1, description="Fields and weights.")
    synthetic: bool | None = Field(default=None, description="True when part of a synthetic asset repo.")


# ---------------------------------------------------------------- evals/slices.yaml


class Slices(_Strict):
    """evals/slices.yaml. Each value of each key is one slice."""

    model_config = ConfigDict(extra="forbid", title="slices.yaml")

    keys: dict[str, list[str]] = Field(description="Slice key to its allowed values, in display order.")
    synthetic: bool | None = Field(default=None, description="True when part of a synthetic asset repo.")


# ---------------------------------------------------------------- policy/gate.yaml

BlockReason = Literal["slice_below_margin", "contamination", "license_denied"]


class Stage(_Strict):
    stub: bool = Field(description="True when the stage is a placeholder and does not run.")


class Stages(_Strict):
    shadow: Stage
    canary: Stage
    bias_tests: Stage


class GatePolicy(_Strict):
    """policy/gate.yaml."""

    model_config = ConfigDict(extra="forbid", title="gate.yaml (policy)")

    margin: float = Field(description="Block when a slice delta is below this (score points).")
    min_n: int = Field(ge=1, description="Slices with fewer rows are flagged low_n and never block.")
    ci: float = Field(gt=0, lt=1, description="Confidence level of the percentile bootstrap CI.")
    n_boot: int = Field(ge=100, description="Bootstrap resamples.")
    seed: int = Field(ge=0, description="Seed for the bootstrap RNG.")
    block_on: list[BlockReason] = Field(description="Failed checks with these reasons block the rebase.")
    stages: Stages
    synthetic: bool | None = Field(default=None, description="True when part of a synthetic asset repo.")


# ---------------------------------------------------------------- policy/license.yaml


class LicensePolicy(_Strict):
    """policy/license.yaml. Lowercase SPDX ids of allowed base-model licenses."""

    model_config = ConfigDict(extra="forbid", title="license.yaml (policy)")

    allow: list[str] = Field(description="Allowed licenses, lowercase SPDX ids such as apache-2.0.")
    synthetic: bool | None = Field(default=None, description="True when part of a synthetic asset repo.")


# ---------------------------------------------------------------- scores.json


class HoldoutDigest(_Strict):
    id: str
    rows: int = Field(ge=0)
    sha256: Sha256Hex


class FileDigest(_Strict):
    path: str
    sha256: Sha256Hex


class PredictionDigests(_Strict):
    incumbent: FileDigest
    candidate: FileDigest


class Overall(_Strict):
    n: int = Field(ge=1)
    incumbent: float = Field(description="Mean row score of the incumbent, 0-100.")
    candidate: float = Field(description="Mean row score of the candidate, 0-100.")
    delta: float = Field(description="candidate - incumbent, score points.")
    ci_low: float
    ci_high: float


class SliceResult(Overall):
    key: str
    value: str
    low_n: bool = Field(description="n < min_n. Low-n slices never block.")
    below_margin: bool = Field(description="delta < margin and not low_n.")


class Scores(_Strict):
    """scores.json written by `roadbed score`."""

    model_config = ConfigDict(extra="forbid", title="scores.json")

    spec: SpecVersion
    synthetic: bool
    model: str
    rebase: int = Field(ge=0)
    seed: int
    n_boot: int
    ci: float = Field(description="Confidence level of ci_low/ci_high.")
    holdout: HoldoutDigest
    predictions: PredictionDigests
    notice: str | None = Field(default=None, description="Provenance notice, e.g. that no training ran.")
    overall: Overall
    slices: list[SliceResult]


# ---------------------------------------------------------------- gate.json


class Check(_Strict):
    name: str = Field(description="slice_margin, license, contamination, or stage:<name>.")
    status: Literal["pass", "fail", "warn", "skipped"] = Field(
        description="fail blocks; warn is a failure whose reason is not in block_on; skipped did not run."
    )
    detail: str


class Finding(_Strict):
    id: str = Field(pattern=r"^F-\d{4}$")
    rebase: int
    slice: str = Field(description="key=value")
    n: int
    delta: float
    ci_low: float
    ci_high: float
    margin: float


class GateResult(_Strict):
    """gate.json written by `roadbed gate`."""

    model_config = ConfigDict(extra="forbid", title="gate.json")

    spec: SpecVersion
    synthetic: bool
    rebase: int
    status: Literal["pass", "blocked"]
    policy_sha256: Sha256Hex = Field(description="sha256 of the RFC 8785 canonical JSON of the policy.")
    checks: list[Check]
    findings: list[Finding]


# ---------------------------------------------------------------- gate inputs (not files)


class LicenseDecision(_Strict):
    license: str = Field(description="Lowercase SPDX id of the base model license.")
    allowed: bool
    source: str = Field(default="policy/license.yaml")


class Contamination(_Strict):
    train_manifest: str | None = Field(default=None, description="Path of the train manifest, if present.")
    shared_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- schema export

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "roadbed": RoadbedConfig,
    "holdout-row": HoldoutRow,
    "prediction-row": PredictionRow,
    "scorers": Scorers,
    "slices": Slices,
    "gate-policy": GatePolicy,
    "license-policy": LicensePolicy,
    "scores": Scores,
    "gate": GateResult,
}


def json_schema(name: str) -> dict[str, Any]:
    """Draft 2020-12 JSON Schema for SCHEMA_MODELS[name], as published in spec/schemas/v0.1."""
    model = SCHEMA_MODELS[name]
    body = model.model_json_schema(mode="serialization")
    return {"$schema": SCHEMA_DIALECT, "$id": f"{SCHEMA_BASE_ID}{name}.schema.json", **body}


def export_schemas(out_dir: str | Path) -> list[Path]:
    """Write every schema to out_dir/<name>.schema.json. Returns the paths written."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in SCHEMA_MODELS:
        p = out / f"{name}.schema.json"
        p.write_text(json.dumps(json_schema(name), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        paths.append(p)
    return paths
