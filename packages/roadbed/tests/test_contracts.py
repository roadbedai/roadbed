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
