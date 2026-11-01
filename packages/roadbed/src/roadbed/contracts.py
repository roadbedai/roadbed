"""Pydantic v2 models for every file in the Roadbed v0.1 asset format.

The JSON Schemas in spec/schemas/v0.1 are generated from these models by
`export_schemas`. A test checks that the files on disk match the models.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

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
    sealed_by: ActorId = Field(description="User id of the validator who sealed the holdout.")
    sealed_at: AwareDatetime = Field(description="When the holdout was sealed (RFC 3339, with zone).")


class Data(_Strict):
    holdout: HoldoutRef
    verdicts: str | None = Field(default=None, description="Optional. Prefix (from asset root) of the frozen verdict snapshots, e.g. data/snapshots.")
    snapshot: str | None = Field(default=None, description="Optional. Id of the snapshot the next training run uses, e.g. S-2026-11-01.")


class Evals(_Strict):
    scorers: str = Field(description="Path to scorers.yaml.")
    slices: str = Field(description="Path to slices.yaml.")
    margin: float = Field(description="Most negative allowed slice delta, in score points.")
    min_n: int = Field(ge=1, description="Slices with fewer rows are flagged low_n and never block.")
    golden: str | None = Field(default=None, description="Optional. Path to evals/golden.jsonl.")
    correction_tests: str | None = Field(default=None, description="Optional. Path to evals/correction_tests.jsonl.")


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
    license: str | None = Field(default=None, description="Optional. Path to the license policy, e.g. policy/license.yaml.")
    lineage: str | None = Field(default=None, description="Optional. Path to the lineage edge list, e.g. lineage/edges.jsonl.")


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
    row_hash: Sha256Hex | None = Field(
        default=None,
        description="Optional. Row hash of the source verdict row (see SPEC.md, hashing). When absent, sha256(JCS(row)).",
    )


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


class CostCap(_Strict):
    eur: float = Field(gt=0, description="Abort the rebase when its cost reaches this many euros.")
    gpu_hours: float | None = Field(default=None, gt=0, description="Optional. Abort at this many GPU-hours.")


class ContaminationPolicy(_Strict):
    minhash_threshold: float = Field(gt=0, le=1, description="Near-duplicate when MinHash Jaccard >= this. Plan value 0.8.")
    num_perm: int = Field(ge=16, description="MinHash permutations.")
    shingle: int = Field(ge=1, description="Word shingle size.")


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
    cost_cap: CostCap | None = Field(default=None, description="Optional. Cost cap per rebase.")
    contamination: ContaminationPolicy | None = Field(default=None, description="Optional. Near-duplicate settings.")
    synthetic: bool | None = Field(default=None, description="True when part of a synthetic asset repo.")


# ---------------------------------------------------------------- policy/license.yaml


class LicensePolicy(_Strict):
    """policy/license.yaml. Lowercase SPDX ids of allowed base-model licenses."""

    model_config = ConfigDict(extra="forbid", title="license.yaml (policy)")

    allow: list[str] = Field(description="Allowed licenses, lowercase SPDX ids such as apache-2.0.")
    review: list[str] | None = Field(
        default=None, description="Optional. Licenses that need human review, lowercase ids or globs such as llama*."
    )
    default: Literal["deny", "review"] | None = Field(
        default=None, description="Optional. Decision for a license in neither list. Absent means deny."
    )
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
    detail: str = Field(min_length=1, description="Why. A skipped check says why it was skipped.")


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

    model_config = ConfigDict(extra="forbid", title="gate.json", json_schema_extra={"allOf": [{
        "if": {"properties": {"status": {"const": "pass"}}, "required": ["status"]},
        "then": {"properties": {"checks": {"items": {"properties": {"status": {"not": {"const": "fail"}}}}}}},
    }]})

    spec: SpecVersion
    synthetic: bool
    rebase: int
    status: Literal["pass", "blocked"]
    policy_sha256: Sha256Hex = Field(description="sha256 of the RFC 8785 canonical JSON of the policy.")
    checks: list[Check]
    findings: list[Finding]

    @model_validator(mode="after")
    def _pass_has_no_failed_check(self) -> GateResult:
        if self.status == "pass" and any(c.status == "fail" for c in self.checks):
            raise ValueError("a passing gate has no failed check")
        return self


# ---------------------------------------------------------------- gate inputs (not files)


class LicenseDecision(_Strict):
    license: str = Field(description="Lowercase SPDX id of the base model license.")
    allowed: bool
    source: str = Field(default="policy/license.yaml")


class Contamination(_Strict):
    train_manifest: str | None = Field(default=None, description="Path of the train manifest, if present.")
    shared_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- shared refs (W01)

Oid = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", description="Bucket object id: lowercase hex sha256 of the object bytes.")]
ImageDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$", description="OCI image digest, sha256:<hex>.")]
Revision = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$", description="40-hex commit sha.")]
Role = Literal["developer", "validator", "approver", "admin", "auditor"]
SodRule = Literal["SoD-01", "SoD-02", "SoD-03", "SoD-04", "SoD-05"]
ActorId = Annotated[str, Field(
    pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$",
    description="User or service id (lowercase, digits, _ and -). Never an email address or a name.",
)]
SubjectToken = Annotated[str, Field(pattern=r"^tok_[0-9a-f]{8,64}$", description="HMAC subject token.")]
_EXECUTOR_NO_GPU = {
    "if": {"properties": {"executor": {"properties": {"name": {"enum": ["mock", "replay"]}}}}},
    "then": {"properties": {
        "gpu": {"properties": {"name": {"const": "none"}, "count": {"const": 0}}},
        "cost": {"properties": {"gpu_hours": {"maximum": 0}, "price_per_hour_eur": {"maximum": 0}, "eur": {"maximum": 0}}},
    }},
}


class _Aliased(_Strict):
    """Models with a JSON key that is not a Python name (`_type`, `from`)."""

    model_config = ConfigDict(extra="forbid", serialize_by_alias=True, validate_by_name=True, validate_by_alias=True)


class PinnedBase(_Strict):
    repo: str = Field(description="Hugging Face repo id of the base model.")
    revision: Revision


class RecipeRef(_Strict):
    path: str = Field(description="Recipe path from asset root, e.g. recipes/qwen3/lora-r16-sft@v4.yaml.")
    sha256: Sha256Hex = Field(description="sha256 of the recipe file bytes.")


class DatasetRef(_Strict):
    id: str = Field(description="Snapshot id (S-...) or holdout id (H-...).")
    merkle_root: Sha256Hex = Field(description="Merkle root of the sorted row hashes (SPEC.md, hashing).")
    rows: int | None = Field(default=None, ge=0, description="Optional. Row count.")


class HoldoutSealRef(DatasetRef):
    seal_seq: int = Field(ge=1, description="Audit seq of the seal event (SEAL.json audit_seq).")
    sealed_at: AwareDatetime = Field(description="SEAL.json sealed_at.")
    seal_commit: Revision = Field(description="Asset repo commit that added SEAL.json (git log --diff-filter=A).")


# ---------------------------------------------------------------- roadbed.lock


class LockFile(_Strict):
    path: str = Field(description="Tracked path from asset root, '/' separated.")
    sha256: Sha256Hex
    size: int = Field(ge=0, description="Bytes.")


class LockObject(_Strict):
    oid: Oid
    size: int = Field(ge=0)
    ref: str = Field(description="Tracked path that names this object (a pointer file or a manifest).")


class Lock(_Strict):
    """roadbed.lock: every tracked path and every bucket object, sorted by path."""

    model_config = ConfigDict(extra="forbid", title="roadbed.lock")

    spec: SpecVersion
    files: list[LockFile] = Field(description="Every tracked file except roadbed.lock, sorted by path (bytewise).")
    objects: list[LockObject] = Field(description="Every bucket object the tree names, sorted by oid.")
    synthetic: bool | None = None


class ObjectPointer(_Strict):
    """<path>.ptr: stands in git for a file over 1 MB that lives in the bucket."""

    model_config = ConfigDict(extra="forbid", title="Object pointer")

    spec: SpecVersion
    oid: Oid
    size: int = Field(gt=1_000_000, description="Bytes. Only files over 1 MB become pointers.")
    media_type: str | None = None


# ---------------------------------------------------------------- data/snapshots/<date>/


class SnapshotPart(_Strict):
    oid: Oid = Field(description="sha256 of the part bytes; the object key is objects/sha256/ab/cd/<oid>.")
    rows: int = Field(ge=0)
    size: int = Field(ge=0)
    path: str | None = Field(default=None, description="Optional. Repo path when the part is small enough to live in git.")


class Pseudonymisation(_Strict):
    profile: str = Field(description="Pseudonymisation profile id applied by the SDK, e.g. insurer-default@v1.")
    hmac_key_id: str = Field(description="Id of the tenant HMAC key. The key itself is never stored.")


class SnapshotManifest(_Strict):
    """data/snapshots/<date>/MANIFEST.json: one frozen training snapshot."""

    model_config = ConfigDict(extra="forbid", title="Snapshot MANIFEST.json")

    spec: SpecVersion
    id: str = Field(pattern=r"^S-\d{4}-\d{2}-\d{2}(-[a-z0-9]+)?$", description="Snapshot id, S-<date>.")
    created_at: AwareDatetime
    rows: int = Field(ge=0)
    schema_sha256: Sha256Hex = Field(description="sha256(JCS(output schema)) the verdicts validate against.")
    merkle_root: Sha256Hex = Field(description="Merkle root of the sorted row hashes of every row in the parts.")
    parts: list[SnapshotPart]
    label_rules: str = Field(description="Label resolution rules and version, e.g. label_rules@v1.")
    pseudonymisation: Pseudonymisation
    parent: str | None = Field(default=None, description="Optional. Snapshot this one replaces (e.g. after an erasure).")
    synthetic: bool


class SnapshotRow(_Strict):
    """One line of a snapshot part: a resolved verdict row. Training reads these."""

    model_config = ConfigDict(extra="forbid", title="Snapshot row")

    interaction_id: str = Field(min_length=1)
    input_ref: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output: dict[str, Any] = Field(description="Model output at capture time.")
    verdict: dict[str, Any] = Field(description="Resolved label. Validates against task.output_schema.")
    reason_code: str | None = None
    outcome: dict[str, Any] | None = Field(default=None, description="Late system-of-record outcome, if any.")
    ts: AwareDatetime = Field(description="Verdict time.")
    slices: dict[str, str]
    row_hash: Sha256Hex = Field(description="sha256(JCS({interaction_id, input_ref, output, verdict, reason_code, outcome, ts})).")


# ---------------------------------------------------------------- data/holdout/<id>/SEAL.json


class Stratification(_Strict):
    keys: list[str] = Field(description="Slice keys the sample is stratified on.")
    method: Literal["proportional", "equal"]
    fraction: float | None = Field(default=None, gt=0, le=1)


class Seal(_Strict):
    """data/holdout/<id>/SEAL.json. Written once, by a validator."""

    model_config = ConfigDict(
        extra="forbid",
        title="SEAL.json",
        json_schema_extra={"dependentRequired": {
            k: [x for x in ("merkle_root", "minhash_oid", "snapshot", "stratification") if x != k]
            for k in ("merkle_root", "minhash_oid", "snapshot", "stratification")
        }},
    )

    id: str = Field(description="Holdout id, e.g. H-2026-11-01.")
    sealed_by: ActorId = Field(description="User id of the validator who sealed it (SoD-05).")
    sealed_at: AwareDatetime
    audit_seq: int = Field(ge=1, description="Audit seq of the seal event. Training must have a larger seq.")
    seed: int = Field(ge=0, description="Sampling seed.")
    rows: int = Field(ge=0)
    sha256: Sha256Hex = Field(description="sha256 of holdout.jsonl bytes.")
    synthetic: bool
    seal_commit: Revision | None = Field(
        default=None,
        description="Optional, informational. verify takes the seal commit from git (the commit that added SEAL.json), never from here.",
    )
    stratification: Stratification | None = None
    merkle_root: Sha256Hex | None = Field(default=None, description="Optional. Merkle root of the holdout row hashes.")
    minhash_oid: Oid | None = Field(default=None, description="Optional. Bucket object with the holdout MinHash signatures.")
    snapshot: str | None = Field(default=None, description="Optional. Snapshot the rows were drawn from.")

    @model_validator(mode="after")
    def _full_or_day0(self) -> Seal:
        group = (self.merkle_root, self.minhash_oid, self.snapshot, self.stratification)
        if any(v is not None for v in group) and any(v is None for v in group):
            raise ValueError("merkle_root, minhash_oid, snapshot and stratification come together (a Day 0 seal has none)")
        return self


# ---------------------------------------------------------------- data/tombstones.jsonl


class TombstoneRow(_Strict):
    """One line of data/tombstones.jsonl: an erasure request."""

    model_config = ConfigDict(extra="forbid", title="Tombstone row")

    subject_token: SubjectToken
    requested_at: AwareDatetime
    requested_by: ActorId
    reason: Literal["gdpr_art17", "retention", "other"]
    audit_seq: int = Field(ge=1)
    rows_removed: int = Field(ge=0)
    snapshot: str = Field(description="New snapshot id written without the subject's rows.")


# ---------------------------------------------------------------- evals/*.jsonl


class GoldenRow(_Strict):
    """One line of evals/golden.jsonl."""

    model_config = ConfigDict(extra="forbid", title="Golden row")

    id: str = Field(min_length=1)
    slices: dict[str, str]
    input_ref: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expected: dict[str, Any]
    row_hash: Sha256Hex | None = None


class CorrectionTestRow(_Strict):
    """One line of evals/correction_tests.jsonl: one per reviewer fix."""

    model_config = ConfigDict(extra="forbid", title="Correction test row")

    id: str = Field(pattern=r"^CT-\d{4,}$")
    interaction_id: str
    input_ref: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    slices: dict[str, str]
    model_version: str = Field(description="Version that produced the wrong output.")
    model_output: dict[str, Any]
    expected: dict[str, Any] = Field(description="Reviewer fix.")
    reason_code: str
    created_by: ActorId
    created_at: AwareDatetime
    row_hash: Sha256Hex | None = None


# ---------------------------------------------------------------- recipes/*.yaml


class Recipe(_Strict):
    """recipes/<family>/<name>@<version>.yaml."""

    model_config = ConfigDict(extra="forbid", title="Recipe")

    recipe: str = Field(description="Recipe name, <family>/<name>.")
    version: str
    trainer: str = Field(description="e.g. unsloth or trl.SFTTrainer.")
    method: Literal["lora", "qlora"]
    r: int = Field(ge=1)
    alpha: int = Field(ge=1)
    lr: float = Field(gt=0)
    epochs: float = Field(gt=0)
    seed: int = Field(ge=0)
    family: str | None = None
    targets: list[str] | None = Field(default=None, description="LoRA target modules.")
    dropout: float | None = Field(default=None, ge=0, lt=1)
    max_seq_len: int | None = Field(default=None, ge=1)
    batch_size: int | None = Field(default=None, ge=1)
    grad_accum: int | None = Field(default=None, ge=1)
    chat_template: str | None = None
    image: ImageDigest | None = Field(default=None, description="Pinned trainer image digest.")
    fallback: str | None = Field(default=None, description="Fallback trainer, e.g. trl+peft.")
    stub: bool | None = None
    synthetic: bool | None = None
    note: str | None = None


# ---------------------------------------------------------------- receipts (in-toto Statement v1)


class Subject(_Strict):
    name: str = Field(description="Repo path, or oid:<hex> for a bucket object.")
    digest: dict[Literal["sha256"], Sha256Hex]


class JobRef(_Strict):
    id: str
    kind: Literal["train", "eval", "bench", "rebase"]


class Gpu(_Strict):
    name: str = Field(description="e.g. NVIDIA H100 80GB HBM3; 'none' for the mock executor.")
    count: int = Field(ge=0)
    driver: str | None = None


class RunCost(_Strict):
    gpu_hours: float = Field(ge=0)
    price_per_hour_eur: float = Field(ge=0)
    eur: float = Field(ge=0)


ExecutorName = Literal["runner", "mock", "replay", "hf-jobs", "together"]


class ExecutorRef(_Strict):
    name: ExecutorName = Field(description="Who ran the job. 'mock' and 'replay' never produce demo numbers.")
    version: str
    host: str | None = None


class RunPredicate(_Strict):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"allOf": [_EXECUTOR_NO_GPU]})

    job: JobRef
    model: str
    rebase: int | None = None
    base: PinnedBase = Field(description="Base model repo and exact revision sha.")
    recipe: RecipeRef = Field(description="Recipe path and file hash.")
    snapshot: DatasetRef = Field(description="Training snapshot id and Merkle root.")
    holdout: HoldoutSealRef = Field(description="Sealed holdout: id, Merkle root, seal seq, sealed_at and seal commit.")
    image_digest: ImageDigest
    uv_lock_sha256: Sha256Hex = Field(description="sha256 of the runner uv.lock bytes.")
    gpu: Gpu
    seed: int
    started_at: AwareDatetime
    ended_at: AwareDatetime
    audit_seq: int = Field(ge=1, description="Audit seq of the job start event. Must exceed holdout.seal_seq.")
    prev_receipt: Sha256Hex | None = Field(default=None, description="sha256(JCS(previous receipt.json)) in this asset, null for the first.")
    cost: RunCost
    executor: ExecutorRef
    synthetic: bool | None = None

    @model_validator(mode="after")
    def _mock_has_no_gpu(self) -> RunPredicate:
        if self.executor.name in ("mock", "replay") and (
            self.gpu.name != "none" or self.gpu.count or self.cost.gpu_hours or self.cost.price_per_hour_eur or self.cost.eur
        ):
            raise ValueError("a mock or replay run has gpu 'none' and zero cost")
        return self


class Receipt(_Aliased):
    """receipt.json: in-toto Statement v1 with predicate type https://roadbed.dev/run/v1."""

    model_config = ConfigDict(
        extra="forbid", serialize_by_alias=True, validate_by_name=True, validate_by_alias=True, title="receipt.json"
    )

    type_: Literal["https://in-toto.io/Statement/v1"] = Field(alias="_type")
    subject: list[Subject] = Field(min_length=1)
    predicateType: Literal["https://roadbed.dev/run/v1"]  # in-toto key name
    predicate: RunPredicate


class Signature(_Strict):
    """receipt.sig and MANIFEST.sig: one ed25519 signature."""

    model_config = ConfigDict(extra="forbid", title="Signature (.sig)")

    alg: Literal["ed25519"]
    keyid: Sha256Hex = Field(description="sha256 of the raw 32-byte public key.")
    key: str = Field(description="Public key path from asset root, e.g. policy/keys/receipt.pub.")
    signed: str = Field(description="Path of the signed file from asset root.")
    payload: Literal["jcs", "raw"] = Field(description="jcs: the JCS bytes of the signed JSON file; raw: its bytes.")
    sig: str = Field(pattern=r"^[A-Za-z0-9+/]{86}==$", description="Base64 (RFC 4648, padded) 64-byte signature.")


# ---------------------------------------------------------------- versions/vN/model.json


class Adapter(_Strict):
    oid: Oid
    size: int = Field(ge=0)
    format: Literal["peft-lora"]


class ModelVersion(_Strict):
    """versions/vN/model.json."""

    model_config = ConfigDict(extra="forbid", title="model.json")

    spec: SpecVersion
    model: str
    version: str = Field(pattern=r"^v\d+$")
    base: PinnedBase
    license: str = Field(description="Lowercase SPDX id of the base license.")
    adapter: Adapter
    recipe: RecipeRef
    snapshot: DatasetRef
    holdout: DatasetRef
    created_at: AwareDatetime
    receipt: str = Field(description="Path of the training receipt.json.")
    rebase: int | None = Field(default=None, description="Rebase that produced this version, if any.")
    synthetic: bool


# ---------------------------------------------------------------- rebases/<n>/rebase.yaml

RebaseState = Literal[
    "detected", "license", "seal_check", "training", "predicting", "benchmarking", "gating", "evidence",
    "pr_opened", "awaiting_signoff", "blocked", "approved", "promoted", "rolled_back", "failed",
]


class VersionBase(_Strict):
    version: str
    base: str


class RebaseBase(_Strict):
    repo: str
    revision: str = Field(pattern=r"^([0-9a-f]{40}|unpinned)$")
    license: str | None = None


class PullRequestRef(_Strict):
    number: int = Field(ge=1)
    url: str


class RebaseMeta(_Aliased):
    """rebases/<n>/rebase.yaml. Day 0 fields plus optional orchestrator state."""

    model_config = ConfigDict(
        extra="forbid", serialize_by_alias=True, validate_by_name=True, validate_by_alias=True, title="rebase.yaml"
    )

    rebase: int = Field(ge=0)
    model: str
    base: RebaseBase
    created_at: AwareDatetime
    synthetic: bool | None = None
    from_: VersionBase | None = Field(default=None, alias="from")
    to: VersionBase | None = None
    trigger: Literal["watch", "manual", "api"] | None = None
    materiality: Literal["material", "non_material"] | None = Field(default=None, description="Base change is material by default.")
    mode: Literal["from-predictions", "train"] | None = None
    note: str | None = None
    state: RebaseState | None = None
    requested_by: ActorId | None = None
    holdout: str | None = Field(default=None, description="Holdout id.")
    snapshot: str | None = Field(default=None, description="Training snapshot id.")
    recipe: str | None = Field(default=None, description="Recipe path.")
    pr: PullRequestRef | None = None


# ---------------------------------------------------------------- rebases/<n>/contamination.json


class ExactOverlap(_Strict):
    shared: int = Field(ge=0)
    row_hashes: list[Sha256Hex]


class NearPair(_Strict):
    holdout: Sha256Hex
    train: Sha256Hex
    jaccard: float = Field(ge=0, le=1)


class NearOverlap(_Strict):
    method: Literal["minhash", "jaccard"] = Field(description="minhash estimate, or exact jaccard for small sets.")
    num_perm: int
    threshold: float
    max_jaccard: float = Field(ge=0, le=1)
    pairs: list[NearPair] = Field(description="Pairs with jaccard >= threshold.")


class SealOrder(_Strict):
    """The three legs of 'seal precedes training'. The inputs live in SEAL.json, the receipt and git; verify recomputes."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"allOf": [{
        "if": {"properties": {"ok": {"const": True}}, "required": ["ok"]},
        "then": {"properties": {"audit_seq": {"const": True}, "timestamp": {"const": True}, "git_ancestor": {"const": True}}},
    }]})

    seal_commit: Revision = Field(description="Commit that added SEAL.json: git log --diff-filter=A --format=%H -- <SEAL.json>.")
    train_commit: Revision = Field(description="Commit that added the training receipt.json.")
    audit_seq: bool = Field(description="SEAL.json audit_seq < receipt predicate.audit_seq.")
    timestamp: bool = Field(description="SEAL.json sealed_at < receipt predicate.started_at.")
    git_ancestor: bool = Field(description="seal_commit is an ancestor of train_commit.")
    ok: bool = Field(description="All three legs are true.")

    @model_validator(mode="after")
    def _ok_is_all_legs(self) -> SealOrder:
        if self.ok != (self.audit_seq and self.timestamp and self.git_ancestor):
            raise ValueError("ok must equal audit_seq and timestamp and git_ancestor")
        return self


class ContaminationReport(_Strict):
    """rebases/<n>/contamination.json."""

    model_config = ConfigDict(extra="forbid", title="contamination.json", json_schema_extra={"allOf": [{
        "if": {"properties": {"status": {"const": "pass"}}, "required": ["status"]},
        "then": {"properties": {
            "exact": {"properties": {"shared": {"const": 0}, "row_hashes": {"maxItems": 0}}},
            "near": {"properties": {"pairs": {"maxItems": 0}}},
            "seal_order": {"properties": {"ok": {"const": True}}},
        }},
    }]})

    spec: SpecVersion
    rebase: int
    status: Literal["pass", "fail"]
    holdout: DatasetRef
    train: DatasetRef
    exact: ExactOverlap
    near: NearOverlap
    seal_order: SealOrder
    synthetic: bool

    @model_validator(mode="after")
    def _pass_means_clean(self) -> ContaminationReport:
        clean = self.exact.shared == 0 and not self.exact.row_hashes and not self.near.pairs and self.seal_order.ok
        if self.status == "pass" and not clean:
            raise ValueError("status pass needs no shared rows, no near pairs and a seal order that holds")
        return self


# ---------------------------------------------------------------- rebases/<n>/cost.json


class PhaseCost(_Strict):
    gpu_hours: float = Field(ge=0)
    eur: float = Field(ge=0)


class Bench(_Strict):
    prompts: int = Field(ge=1)
    concurrency: int = Field(ge=1)
    p50_ms: float = Field(ge=0)
    p95_ms: float = Field(ge=0)
    tokens_per_s: float = Field(ge=0)
    eur_per_1k_calls: float = Field(ge=0)


class Vllm(_Strict):
    version: str
    flags: list[str]


class CostReport(_Strict):
    """rebases/<n>/cost.json."""

    model_config = ConfigDict(extra="forbid", title="cost.json", json_schema_extra={"allOf": [{
        "if": {"properties": {"executor": {"enum": ["mock", "replay"]}}, "required": ["executor"]},
        "then": {"properties": {
            "gpu": {"properties": {"name": {"const": "none"}, "count": {"const": 0}}},
            "price_per_hour_eur": {"maximum": 0},
            "train": {"properties": {"gpu_hours": {"maximum": 0}, "eur": {"maximum": 0}}},
            "predict": {"properties": {"gpu_hours": {"maximum": 0}, "eur": {"maximum": 0}}},
            "bench": {"properties": {"eur_per_1k_calls": {"maximum": 0}}},
            "total_eur": {"maximum": 0},
        }},
    }]})

    spec: SpecVersion
    rebase: int
    executor: ExecutorName
    gpu: Gpu
    price_per_hour_eur: float = Field(ge=0)
    train: PhaseCost
    predict: PhaseCost
    bench: Bench
    vllm: Vllm
    total_eur: float = Field(ge=0)
    cap_eur: float | None = None
    synthetic: bool

    @model_validator(mode="after")
    def _mock_has_no_cost(self) -> CostReport:
        costs = (self.price_per_hour_eur, self.train.gpu_hours, self.train.eur, self.predict.gpu_hours,
                 self.predict.eur, self.bench.eur_per_1k_calls, self.total_eur)
        if self.executor in ("mock", "replay") and (self.gpu.name != "none" or self.gpu.count or any(costs)):
            raise ValueError("a mock or replay run has gpu 'none' and zero cost")
        return self


# ---------------------------------------------------------------- rebases/<n>/license.json


class LicenseDecisionRecord(_Strict):
    """rebases/<n>/license.json."""

    model_config = ConfigDict(extra="forbid", title="license.json")

    spec: SpecVersion
    rebase: int
    base: PinnedBase
    license: str = Field(description="Lowercase SPDX id read from the model card or LICENSE file.")
    source: Literal["cardData", "LICENSE", "none"]
    decision: Literal["allow", "review", "deny"]
    policy: str = Field(description="Policy path, e.g. policy/license.yaml.")
    policy_sha256: Sha256Hex = Field(description="sha256(JCS(policy)) with null fields left out.")
    decided_at: AwareDatetime
    synthetic: bool


# ---------------------------------------------------------------- approvals


ApprovalReason = Literal[
    "no_findings", "findings_noted", "low_n_slices", "slice_regression", "license_review",
    "contamination_risk", "cost_over_cap", "other",
]
ApprovalRef = Annotated[str, Field(
    pattern=r"^(F-\d{4}|CT-\d{4,}|audit:\d+|rebase:\d+)$", description="A finding, correction test, audit seq or rebase id."
)]


class ApprovalEntry(_Strict):
    role: Literal["validator", "approver"]
    decision: Literal["validated", "validated_with_findings", "approved", "rejected"]
    reason_code: ApprovalReason
    refs: list[ApprovalRef] = Field(default_factory=list, description="Ids the decision rests on. No free text.")
    at: AwareDatetime
    audit_seq: int = Field(ge=1)
    pack_version: int = Field(ge=1, description="Evidence pack version this approval signed.")


class Approval(ApprovalEntry):
    """One sign-off. Also the response of POST /v1/rebases/{n}/approvals."""

    model_config = ConfigDict(extra="forbid", title="Approval")

    rebase: int
    actor: ActorId


class Approvals(_Strict):
    """rebases/<n>/evidence/approvals.json."""

    model_config = ConfigDict(extra="forbid", title="approvals.json")

    spec: SpecVersion
    rebase: int
    status: Literal["awaiting_signoff", "approved", "rejected", "blocked"]
    status_text: str = Field(description="Display text, e.g. 'awaiting MRM sign-off'.")
    required: list[Literal["validator", "approver"]]
    pack_version: int = Field(ge=1, description="Each approval writes a new signed pack version.")
    approvals: dict[ActorId, ApprovalEntry] = Field(
        description="Keyed by actor id, so one actor holds one role: validator and approver differ (SoD-03).",
        json_schema_extra={"additionalProperties": False},
    )
    synthetic: bool


# ---------------------------------------------------------------- findings/F-NNNN.yaml


class FindingRecord(_Strict):
    """findings/F-NNNN.yaml. Day 0 shape plus optional status and kind."""

    model_config = ConfigDict(extra="forbid", title="Finding (findings/F-NNNN.yaml)")

    id: str = Field(pattern=r"^F-\d{4}$")
    rebase: int
    kind: Literal["slice_below_margin", "contamination", "license", "seal_order", "other"] | None = None
    status: Literal["open", "accepted", "resolved"] | None = None
    slice: str | None = Field(default=None, description="key=value")
    n: int | None = None
    delta: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    margin: float | None = None
    gate: str | None = Field(default=None, description="Path of the gate.json that raised it.")
    placeholder: bool | None = Field(default=None, description="True for a reserved id with no numbers.")
    note: str | None = None
    synthetic: bool | None = None


# ---------------------------------------------------------------- lineage/edges.jsonl


_NODE = r"^((snapshot|holdout|version|rebase|recipe|base):[A-Za-z0-9._/@+-]{1,128}|subject:tok_[0-9a-f]{8,64})$"


class LineageEdge(_Aliased):
    """One line of lineage/edges.jsonl. Append-only."""

    model_config = ConfigDict(
        extra="forbid", serialize_by_alias=True, validate_by_name=True, validate_by_alias=True, title="Lineage edge"
    )

    from_: str = Field(alias="from", pattern=_NODE)
    to: str = Field(pattern=_NODE)
    kind: Literal["sealed_from", "trained_on", "evaluated_on", "rebased_from", "produced", "promoted_to", "erased_from"]
    hashes: dict[str, Sha256Hex] = Field(description="Digests of the nodes, keyed 'from' and 'to' where known.")
    ts: AwareDatetime
    actor: ActorId


# ---------------------------------------------------------------- audit


class AuditObject(_Strict):
    type: str = Field(description="e.g. rebase, holdout, alias, subject.")
    id: str


AuditRule = Literal["SoD-01", "SoD-02", "SoD-03", "SoD-04", "SoD-05", "RBAC"]
PayloadKey = Annotated[str, Field(pattern=r"^[a-z_]{1,64}$")]
PayloadText = Annotated[str, Field(pattern=r"^[A-Za-z0-9:_./+=-]{1,256}$", max_length=256)]
FREE_TEXT_KEYS = ("note", "comment", "text", "message", "description", "detail", "reason", "name", "email", "phone")


class AuditEvent(_Strict):
    """One audit event. Also one line of GET /v1/audit/export.jsonl."""

    model_config = ConfigDict(extra="forbid", title="Audit event", json_schema_extra={
        "allOf": [{
            "if": {"properties": {"outcome": {"const": "denied"}}, "required": ["outcome"]},
            "then": {"required": ["rule"], "properties": {"rule": {"type": "string"}}},
        }],
    })

    seq: int = Field(ge=1)
    ts: AwareDatetime
    actor: ActorId
    role: Role | Literal["system", "runner"]
    action: str = Field(pattern=r"^[a-z_]+(\.[a-z_]+)+$", description="e.g. holdout.seal, rebase.approve.")
    object: AuditObject
    outcome: Literal["allowed", "denied"]
    rule: AuditRule | None = Field(
        default=None, description="Required when outcome is denied: the SoD rule, or RBAC for a role-matrix deny."
    )
    payload: dict[PayloadKey, PayloadText | int | float | bool | None] = Field(
        description="Ids, tokens, hashes and numbers only. Strings have no spaces and at most 256 characters. "
        "Free-text keys (note, comment, text, ...) are rejected.",
        json_schema_extra={"propertyNames": {"not": {"enum": list(FREE_TEXT_KEYS)}}, "additionalProperties": False},
    )
    prev_hash: Sha256Hex = Field(description="hash of seq-1; 64 zeros for seq 1.")
    hash: Sha256Hex = Field(description="sha256(JCS(event without 'hash')).")

    @model_validator(mode="after")
    def _denied_has_rule(self) -> AuditEvent:
        if self.outcome == "denied" and self.rule is None:
            raise ValueError("a denied event names its rule")
        if set(self.payload) & set(FREE_TEXT_KEYS):
            raise ValueError(f"free-text payload keys are not allowed: {sorted(set(self.payload) & set(FREE_TEXT_KEYS))}")
        return self


class ObjectLock(_Strict):
    bucket: str
    key: str
    version_id: str | None = None


class AuditAnchor(_Strict):
    """audit/anchors/<date>.json: daily head of the audit chain."""

    model_config = ConfigDict(extra="forbid", title="Audit anchor")

    spec: SpecVersion
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    head_seq: int = Field(ge=1)
    head_hash: Sha256Hex
    count: int = Field(ge=0, description="Events appended on this date.")
    prev_anchor: Sha256Hex | None = Field(default=None, description="sha256(JCS(previous anchor file)), null for the first.")
    anchored_at: AwareDatetime
    object_lock: ObjectLock
    synthetic: bool | None = None


# ---------------------------------------------------------------- MANIFEST.sha256 (parsed)


class ManifestEntry(_Strict):
    sha256: Sha256Hex
    path: str = Field(description="Path from asset root.")


class EvidenceManifest(_Strict):
    """Parsed form of a MANIFEST.sha256 text file (rebase or evidence): one entry per '<sha256>  <path>' line."""

    model_config = ConfigDict(extra="forbid", title="MANIFEST.sha256 (parsed)")

    entries: list[ManifestEntry] = Field(min_length=1)


# ---------------------------------------------------------------- runner jobs (control plane <-> runner)


class TrainSpec(_Strict):
    kind: Literal["train"]
    base: PinnedBase
    recipe: RecipeRef
    snapshot: DatasetRef
    holdout: HoldoutSealRef = Field(description="The runner refuses to train when seal order or disjointness fails.")
    seed: int
    image_digest: ImageDigest
    adapter_name: str


class EvalSpec(_Strict):
    kind: Literal["eval"]
    base: PinnedBase
    adapter: Oid | None = Field(default=None, description="Adapter oid; absent or null runs the base model alone.")
    role: Literal["incumbent", "candidate"]
    dataset: DatasetRef = Field(description="The sealed holdout.")
    output_schema_sha256: Sha256Hex = Field(description="sha256(JCS(output schema)); used for guided JSON.")
    max_tokens: int = Field(ge=1)
    temperature: float = Field(ge=0)


class BenchSpec(_Strict):
    kind: Literal["bench"]
    base: PinnedBase
    adapter: Oid | None = None
    prompts: int = Field(ge=1, description="Plan value 500.")
    concurrency: int = Field(ge=1, description="Plan value 32.")
    max_tokens: int = Field(ge=1)
    price_per_hour_eur: float = Field(ge=0)


class ExecutorJob(_Strict):
    """A unit of GPU work. GET /v1/jobs/next returns one."""

    model_config = ConfigDict(extra="forbid", title="Executor job")

    id: str = Field(min_length=1)
    model: str
    rebase: int | None = None
    spec: Annotated[TrainSpec | EvalSpec | BenchSpec, Field(discriminator="kind")]
    cost_cap_eur: float | None = Field(default=None, gt=0)
    created_at: AwareDatetime
    requested_by: ActorId


class JobStatus(_Strict):
    """Body of POST /v1/jobs/{id}/status and the Executor.status return value."""

    model_config = ConfigDict(extra="forbid", title="Job status")

    job_id: str
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    progress: float | None = Field(default=None, ge=0, le=1)
    message: str | None = None
    updated_at: AwareDatetime


class Artifact(_Strict):
    name: str = Field(description="e.g. adapter, predictions.candidate.jsonl, cost.json.")
    oid: Oid
    size: int = Field(ge=0)


class JobResult(_Strict):
    """Body of POST /v1/jobs/{id}/result and the Executor.result return value."""

    model_config = ConfigDict(
        extra="forbid",
        title="Job result",
        json_schema_extra={"allOf": [{
            "if": {"properties": {"state": {"const": "succeeded"}}, "required": ["state"]},
            "then": {"required": ["receipt", "receipt_sig"],
                     "properties": {"receipt": {"type": "object"}, "receipt_sig": {"type": "object"}}},
        }]},
    )

    job_id: str
    state: Literal["succeeded", "failed", "cancelled"]
    artifacts: list[Artifact]
    receipt: Receipt | None = Field(default=None, description="Required when state is succeeded.")
    receipt_sig: Signature | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    error: str | None = None
    finished_at: AwareDatetime

    @model_validator(mode="after")
    def _receipt_when_succeeded(self) -> JobResult:
        if self.state == "succeeded" and (self.receipt is None or self.receipt_sig is None):
            raise ValueError("receipt and receipt_sig are required when state is succeeded")
        return self


@runtime_checkable
class Executor(Protocol):
    """Runs ExecutorJobs. Implementations: runner (GPU box), mock, replay; hf-jobs and together are stubs.

    Every JobResult with state 'succeeded' carries a Receipt whose predicate.executor.name is the
    implementation's name, so mock output can never pass as a real run.
    """

    name: ExecutorName

    def submit(self, job: ExecutorJob) -> str:
        """Queue the job. Returns the job id. Idempotent on job.id."""
        ...

    def status(self, job_id: str) -> JobStatus: ...

    def result(self, job_id: str) -> JobResult:
        """Final result. Raises LookupError for an unknown id and RuntimeError while the job still runs."""
        ...

    def cancel(self, job_id: str) -> None: ...


# ---------------------------------------------------------------- gateway alias config


class Canary(_Strict):
    weight: float = Field(ge=0, le=1, description="Share of traffic, sticky by xxhash(salt + interaction id).")
    salt: str
    adapter: str
    version: str


class Shadow(_Strict):
    target: str = Field(description="Upstream URL or alias that gets a fire-and-forget copy.")


class AliasRoute(_Strict):
    upstream: str = Field(description="OpenAI-compatible base URL, e.g. http://vllm:8000/v1.")
    adapter: str = Field(description="LoRA adapter name loaded in vLLM.")
    version: str = Field(description="Model version sent in x-roadbed-model-version.")
    canary: Canary | None = None
    shadow: Shadow | None = None


class AliasConfig(_Strict):
    """GET /v1/aliases body (with ETag). The gateway polls it every 2 s."""

    model_config = ConfigDict(extra="forbid", title="Alias config")

    spec: SpecVersion
    generation: int = Field(ge=0, description="Increments on every change; the ETag is derived from it.")
    aliases: dict[str, AliasRoute]


# ---------------------------------------------------------------- ingest (SDK -> control plane)


class InteractionIn(_Strict):
    """POST /v1/interactions body. Pseudonymised client-side; 422 without profile_id."""

    model_config = ConfigDict(extra="forbid", title="Interaction (in)")

    interaction_id: str = Field(min_length=1)
    model: str
    alias: str | None = None
    version: str
    ts: AwareDatetime
    input_ref: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    input: dict[str, Any] = Field(description="Pseudonymised input (tokens and placeholders such as <PERSON_1>).")
    output: dict[str, Any]
    profile_id: str = Field(min_length=1, description="Pseudonymisation profile applied by the SDK.")
    hmac_key_id: str = Field(min_length=1)
    subject_token: SubjectToken | None = None
    slices: dict[str, str] | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    synthetic: bool | None = None


class VerdictIn(_Strict):
    """POST /v1/verdicts body. A 'fix' carries the corrected output in verdict."""

    model_config = ConfigDict(
        extra="forbid",
        title="Verdict (in)",
        json_schema_extra={"allOf": [{
            "if": {"properties": {"action": {"const": "fix"}}, "required": ["action"]},
            "then": {"required": ["verdict", "reason_code"],
                     "properties": {"verdict": {"type": "object"}, "reason_code": {"type": "string"}}},
        }]},
    )

    interaction_id: str = Field(min_length=1)
    action: Literal["approve", "fix", "reject", "escalate"]
    verdict: dict[str, Any] | None = Field(default=None, description="Required when action is fix.")
    reason_code: str | None = Field(default=None, description="Required when action is fix.")
    ts: AwareDatetime

    @model_validator(mode="after")
    def _fix_carries_verdict(self) -> VerdictIn:
        if self.action == "fix" and (self.verdict is None or self.reason_code is None):
            raise ValueError("a fix needs verdict and reason_code")
        return self


class OutcomeIn(_Strict):
    """POST /v1/outcomes body: a late system-of-record outcome."""

    model_config = ConfigDict(extra="forbid", title="Outcome (in)")

    interaction_id: str = Field(min_length=1)
    outcome: dict[str, Any]
    source: Literal["system_of_record", "csv"]
    observed_at: AwareDatetime


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
    # W01: the rest of the asset tree
    "lock": Lock,
    "object-pointer": ObjectPointer,
    "snapshot-manifest": SnapshotManifest,
    "snapshot-row": SnapshotRow,
    "seal": Seal,
    "tombstone-row": TombstoneRow,
    "golden-row": GoldenRow,
    "correction-test-row": CorrectionTestRow,
    "recipe": Recipe,
    "model-version": ModelVersion,
    "receipt": Receipt,
    "signature": Signature,
    "rebase": RebaseMeta,
    "contamination": ContaminationReport,
    "cost": CostReport,
    "license-decision": LicenseDecisionRecord,
    "approvals": Approvals,
    "finding": FindingRecord,
    "lineage-edge": LineageEdge,
    "audit-anchor": AuditAnchor,
    "evidence-manifest": EvidenceManifest,
    # W01: control plane, runner, gateway and SDK contracts
    "audit-event": AuditEvent,
    "approval": Approval,
    "executor-job": ExecutorJob,
    "job-status": JobStatus,
    "job-result": JobResult,
    "alias-config": AliasConfig,
    "interaction-in": InteractionIn,
    "verdict-in": VerdictIn,
    "outcome-in": OutcomeIn,
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
