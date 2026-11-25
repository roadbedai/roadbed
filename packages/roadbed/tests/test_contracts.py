"""Schema round-trip: spec files match the models, and instances validate both ways."""

from __future__ import annotations

import json

import jsonschema
import pytest

from conftest import EXAMPLE, SPEC_DIR
from roadbed import io
from roadbed.contracts import SCHEMA_MODELS, json_schema

SCHEMA_DIR = SPEC_DIR / "schemas" / "v0.1"
CONFORMANCE = SPEC_DIR / "conformance"

# Day 0 schemas -> example instance(s) in spec/examples/claims-triage.
# Every schema also has spec/conformance/valid/<name>/*.json (see test_spec_examples.py for the fixtures).
EXAMPLES = {
    "roadbed": [("yaml", "roadbed.yaml")],
    "holdout-row": [("jsonl", "data/holdout/H-EXAMPLE/holdout.jsonl")],
    "prediction-row": [
        ("jsonl", "rebases/1/predictions.incumbent.jsonl"),
        ("jsonl", "rebases/1/predictions.candidate.jsonl"),
    ],
    "scorers": [("yaml", "evals/scorers.yaml")],
    "slices": [("yaml", "evals/slices.yaml")],
    "gate-policy": [("yaml", "policy/gate.yaml")],
    "license-policy": [("yaml", "policy/license.yaml")],
    "scores": [("json", "rebases/1/scores.json")],
    "gate": [("json", "rebases/1/gate.json")],
}


def _instances(name):
    for f in sorted((CONFORMANCE / "valid" / name).glob("*.json")):
        yield json.loads(f.read_text())
    for kind, rel in EXAMPLES.get(name, []):
        p = EXAMPLE / rel
        if kind == "yaml":
            yield json.loads(json.dumps(io.read_yaml(p)))
        elif kind == "json":
            yield json.loads(p.read_text())
        else:
            yield from io.read_jsonl(p)


def _validator(schema):
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)


def test_every_model_has_an_example():
    assert set(EXAMPLES) <= set(SCHEMA_MODELS)
    for name in SCHEMA_MODELS:
        assert next(_instances(name), None) is not None, f"no valid conformance case for {name}"


@pytest.mark.parametrize("name", list(SCHEMA_MODELS))
def test_spec_file_matches_model(name):
    on_disk = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text())
    assert on_disk == json_schema(name), f"regenerate with roadbed.contracts.export_schemas('{SCHEMA_DIR}')"


@pytest.mark.parametrize("name", list(SCHEMA_MODELS))
def test_round_trip(name):
    model = SCHEMA_MODELS[name]
    validator = _validator(json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text()))
    n = 0
    for inst in _instances(name):
        validator.validate(inst)  # example conforms to the spec file
        obj = model.model_validate(inst)  # and to the model
        dumped = obj.model_dump(mode="json", exclude_none=True)
        validator.validate(dumped)  # model output conforms to the spec file
        assert model.model_validate(dumped) == obj
        assert json.loads(obj.model_dump_json(exclude_none=True)) == dumped  # JSON text round trip
        n += 1
    assert n > 0


@pytest.mark.parametrize("name", list(SCHEMA_MODELS))
def test_schema_rejects_unknown_top_level_key(name):
    validator = _validator(json_schema(name))
    inst = next(_instances(name))
    assert not validator.is_valid({**inst, "unexpected_key": 1})


def test_example_outputs_match_task_schema():
    out_schema = json.loads((EXAMPLE / "evals/output.schema.json").read_text())
    v = _validator(out_schema)
    for row in _instances("holdout-row"):
        v.validate(row["expected"])
    for row in _instances("prediction-row"):
        v.validate(row["output"])


def test_roadbed_revision_pattern():
    v = _validator(json_schema("roadbed"))
    inst = next(_instances("roadbed"))
    assert v.is_valid({**inst, "base": {"repo": "Qwen/Qwen3.8-27B", "revision": "a" * 40}})
    assert not v.is_valid({**inst, "base": {"repo": "Qwen/Qwen3.8-27B", "revision": "main"}})


def test_example_marked_synthetic():
    assert (EXAMPLE / "SYNTHETIC.md").exists()
    for name in ("roadbed", "scores", "gate"):
        assert next(_instances(name))["synthetic"] is True


def test_export_schemas_writes_spec_files(tmp_path):
    from roadbed.contracts import export_schemas

    paths = export_schemas(tmp_path)
    assert sorted(p.name for p in paths) == sorted(p.name for p in SCHEMA_DIR.glob("*.schema.json"))
    for p in paths:
        assert p.read_text() == (SCHEMA_DIR / p.name).read_text()


# ---------------------------------------------------------------- contract amendments, round 2


def _case(name, case="example"):
    return json.loads((CONFORMANCE / "valid" / name / f"{case}.json").read_text())


def test_attest_job_parses_as_attest_spec():
    from roadbed.contracts import AttestSpec, ExecutorJob

    job = ExecutorJob.model_validate(_case("executor-job", "attest-rebase"))
    assert isinstance(job.spec, AttestSpec)
    assert job.spec.kind == "rebase" and job.spec.receipt_path == "rebases/13/receipt.json"
    assert job.spec.config is not None and len(job.spec.receipts) == 4


@pytest.mark.parametrize("asset", ["pass", "blocked"])
def test_rebase_receipt_binds_the_eval_config(asset):
    """The fixture rebase receipts carry all six config digests. Five match the files now; the lock digest is the
    roadbed.lock of the commit that added the receipt (the final lock lists the receipt, so it differs)."""
    from roadbed.contracts import Receipt
    from roadbed.hashing import sha256_file

    root = SPEC_DIR / "examples" / asset
    n = 13 if asset == "pass" else 14
    seal = io.read_yaml(root / "roadbed.yaml")["data"]["holdout"]["path"] + "/SEAL.json"
    cfg = Receipt.model_validate_json((root / f"rebases/{n}/receipt.json").read_text()).predicate.config
    assert cfg is not None
    assert cfg.roadbed_yaml_sha256 == sha256_file(root / "roadbed.yaml")
    assert cfg.slices_sha256 == sha256_file(root / "evals/slices.yaml")
    assert cfg.scorers_sha256 == sha256_file(root / "evals/scorers.yaml")
    assert cfg.gate_policy_sha256 == sha256_file(root / "policy/gate.yaml")
    assert cfg.seal_sha256 == sha256_file(root / seal)
    assert cfg.lock_sha256 is not None and cfg.lock_sha256 != sha256_file(root / "roadbed.lock")


def test_verdict_record_is_the_request_plus_a_server_actor():
    from roadbed.contracts import VerdictIn, VerdictRecord

    rec = _case("verdict-record")
    body = {k: v for k, v in rec.items() if k != "actor"}
    assert set(VerdictRecord.model_fields) == set(VerdictIn.model_fields) | {"actor"}
    VerdictIn.model_validate(body)
    with pytest.raises(ValueError):
        VerdictIn.model_validate(rec)  # clients never send actor
    with pytest.raises(ValueError):
        VerdictRecord.model_validate({**rec, "action": "fix", "verdict": None})  # the fix rule is inherited
    assert VerdictRecord.model_validate(rec).actor == "vik"


def test_interaction_id_length():
    from roadbed.contracts import InteractionIn

    body = _case("interaction-in")
    InteractionIn.model_validate({**body, "interaction_id": "i" * 128})
    for bad in ("", "i" * 129):
        with pytest.raises(ValueError):
            InteractionIn.model_validate({**body, "interaction_id": bad})


@pytest.mark.parametrize("value,ok", [
    (9007199254740991, True), (-9007199254740991, True), (9007199254740992, False), (-9007199254740992, False),
    (0.5, True), (1e300, False), (True, True), (None, True),
])
def test_audit_payload_numbers_are_safe_integers(value, ok):
    from roadbed.contracts import AuditEvent

    ev = {**_case("audit-event"), "payload": {"v": value}}
    if ok:
        assert AuditEvent.model_validate(ev).payload["v"] == value
        assert type(AuditEvent.model_validate(ev).payload["v"]) is type(value)
    else:
        with pytest.raises(ValueError):
            AuditEvent.model_validate(ev)


def test_fixture_audit_objects_match_the_patterns():
    """Every audit event in the spec vectors still validates under the object and payload rules."""
    from roadbed.contracts import AuditEvent

    n = 0
    for f in sorted((CONFORMANCE / "vectors").rglob("*.json*")):
        text = f.read_text()
        docs = [json.loads(ln) for ln in text.splitlines() if ln.strip()] if f.suffix == ".jsonl" else [json.loads(text)]
        stack = list(docs)
        while stack:
            d = stack.pop()
            if isinstance(d, dict) and {"seq", "action", "object", "payload", "hash"} <= set(d):
                AuditEvent.model_validate(d)
                n += 1
            elif isinstance(d, dict):
                stack.extend(d.values())
            elif isinstance(d, list):
                stack.extend(d)
    assert n > 0


# ---------------------------------------------------------------- W01d: local seal, unsealed roadbed.yaml


def _local_seal(**kw):
    seal = {k: v for k, v in _case("seal").items() if k != "audit_seq"}
    return {**seal, "provenance": "local", **kw}


def test_local_seal_without_audit_seq_validates():
    from roadbed.contracts import Seal

    inst = _local_seal()
    _validator(json_schema("seal")).validate(inst)
    s = Seal.model_validate(inst)
    assert s.provenance == "local" and s.audit_seq is None


@pytest.mark.parametrize("provenance", [None, "control_plane"])
def test_control_plane_seal_needs_audit_seq(provenance):
    from roadbed.contracts import Seal

    inst = {k: v for k, v in _case("seal").items() if k != "audit_seq"}
    if provenance:
        inst["provenance"] = provenance
    assert not _validator(json_schema("seal")).is_valid(inst)
    with pytest.raises(ValueError, match="audit_seq"):
        Seal.model_validate(inst)
    assert _validator(json_schema("seal")).is_valid({**inst, "audit_seq": 7})


def test_local_seal_with_audit_seq_is_refused():
    from roadbed.contracts import Seal

    inst = _local_seal(audit_seq=7)
    assert not _validator(json_schema("seal")).is_valid(inst)
    with pytest.raises(ValueError, match="audit_seq"):
        Seal.model_validate(inst)


@pytest.mark.parametrize("drop", [("merkle_root", "minhash_oid", "snapshot", "stratification"), ("snapshot",)])
def test_local_seal_needs_the_full_group(drop):
    from roadbed.contracts import Seal

    inst = {k: v for k, v in _local_seal().items() if k not in drop}
    assert not _validator(json_schema("seal")).is_valid(inst)
    with pytest.raises(ValueError):
        Seal.model_validate(inst)


def test_unknown_provenance_is_refused():
    assert not _validator(json_schema("seal")).is_valid(_local_seal(provenance="laptop"))


def test_roadbed_yaml_without_holdout_validates():
    from roadbed.contracts import RoadbedConfig

    inst = next(_instances("roadbed"))
    inst = {**inst, "data": {k: v for k, v in inst["data"].items() if k != "holdout"}}
    _validator(json_schema("roadbed")).validate(inst)
    assert RoadbedConfig.model_validate(inst).data.holdout is None


def test_conformance_has_the_w01d_cases():
    for rel in ("valid/seal/local.json", "valid/roadbed/unsealed.json", "invalid/seal/local-with-audit-seq.json",
                "invalid/seal/local-day0.json", "invalid/seal/bad-provenance.json", "invalid/seal/missing-audit-seq.json",
                "invalid/seal/control-plane-without-audit-seq.json"):
        assert (CONFORMANCE / rel).is_file(), rel


@pytest.mark.parametrize("path,limit", [(("n_boot",), 1_000_000), (("contamination", "num_perm"), 4096),
                                        (("contamination", "shingle"), 64)])
def test_gate_policy_schema_refuses_a_value_over_its_upper_bound(path, limit):
    published = json.loads((SCHEMA_DIR / "gate-policy.schema.json").read_text())
    doc = json.loads(json.dumps(io.read_yaml(SPEC_DIR / "examples/pass/policy/gate.yaml")))
    node = doc
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = limit
    jsonschema.validate(doc, published)
    SCHEMA_MODELS["gate-policy"].model_validate(doc)
    node[path[-1]] = limit + 1
    with pytest.raises(jsonschema.ValidationError, match="maximum"):
        jsonschema.validate(doc, published)
    with pytest.raises(ValueError):
        SCHEMA_MODELS["gate-policy"].model_validate(doc)
