"""spec/examples/{pass,blocked} and spec/conformance: schemas, integrity, determinism, verify."""

from __future__ import annotations

import base64
import fnmatch
import importlib.util
import json
import shutil
from pathlib import Path

import jsonschema
import pytest
import rfc8785
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from typer.testing import CliRunner

from roadbed import io
from roadbed.cli.main import app
from roadbed.contracts import SCHEMA_MODELS, Executor, ExecutorJob, JobResult, JobStatus, json_schema
from roadbed.hashing import sha256_canonical, sha256_file

from conftest import SPEC_DIR

EXAMPLES = SPEC_DIR / "examples"
CONFORMANCE = SPEC_DIR / "conformance"
ASSETS = {"pass": 13, "blocked": 14}
runner = CliRunner()


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, SPEC_DIR / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ref = _load_tool("build_examples")

# path pattern (from asset root) -> (schema name, file kind). Order matters: first match wins.
FILE_SCHEMAS: list[tuple[str, str | None, str]] = [
    ("roadbed.yaml", "roadbed", "yaml"),
    ("roadbed.lock", "lock", "json"),
    ("policy/gate.yaml", "gate-policy", "yaml"),
    ("policy/license.yaml", "license-policy", "yaml"),
    ("policy/keys/*.pub", None, "pem"),
    ("data/snapshots/*/MANIFEST.json", "snapshot-manifest", "json"),
    ("data/snapshots/*/part-*.jsonl", "snapshot-row", "jsonl"),
    ("data/holdout/*/SEAL.json", "seal", "json"),
    ("data/holdout/*/holdout.jsonl", "holdout-row", "jsonl"),
    ("data/tombstones.jsonl", "tombstone-row", "jsonl"),
    ("evals/golden.jsonl", "golden-row", "jsonl"),
    ("evals/correction_tests.jsonl", "correction-test-row", "jsonl"),
    ("evals/scorers.yaml", "scorers", "yaml"),
    ("evals/slices.yaml", "slices", "yaml"),
    ("evals/output.schema.json", None, "json-schema"),
    ("recipes/*.yaml", "recipe", "yaml"),
    ("versions/v*/model.json", "model-version", "json"),
    ("*/receipt.json", "receipt", "json"),
    ("*/receipt.sig", "signature", "json"),
    ("rebases/*/rebase.yaml", "rebase", "yaml"),
    ("rebases/*/predictions.*.jsonl", "prediction-row", "jsonl"),
    ("rebases/*/scores.json", "scores", "json"),
    ("rebases/*/gate.json", "gate", "json"),
    ("rebases/*/contamination.json", "contamination", "json"),
    ("rebases/*/cost.json", "cost", "json"),
    ("rebases/*/license.json", "license-decision", "json"),
    ("rebases/*/bom.cdx.json", None, "cyclonedx"),
    ("rebases/*/report.md", None, "text"),
    ("rebases/*/roadbed.yaml.proposed", "roadbed", "yaml"),
    ("rebases/*/evidence/approvals.json", "approvals", "json"),
    ("*/MANIFEST.sha256", "evidence-manifest", "manifest"),
    ("rebases/*/evidence/MANIFEST.sig", "signature", "json"),
    ("findings/F-*.yaml", "finding", "yaml"),
    ("lineage/edges.jsonl", "lineage-edge", "jsonl"),
    ("audit/anchors/*.json", "audit-anchor", "json"),
    ("*SYNTHETIC.md", None, "text"),
]

# The asset tree of SPEC.md. Both fixtures must hold every entry.
TREE = [
    "roadbed.yaml", "roadbed.lock", "policy/gate.yaml", "policy/license.yaml", "policy/keys/*.pub",
    "data/snapshots/*/MANIFEST.json", "data/holdout/*/SEAL.json", "data/holdout/*/holdout.jsonl",
    "data/tombstones.jsonl", "evals/golden.jsonl", "evals/correction_tests.jsonl", "evals/slices.yaml",
    "evals/scorers.yaml", "evals/output.schema.json", "recipes/*.yaml",
    "versions/v*/model.json", "versions/v*/receipt.json", "versions/v*/receipt.sig",
    *[f"rebases/*/{f}" for f in (
        "rebase.yaml", "receipt.json", "receipt.sig", "predictions.incumbent.jsonl", "predictions.candidate.jsonl",
        "scores.json", "gate.json", "contamination.json", "cost.json", "license.json", "bom.cdx.json", "report.md",
        "MANIFEST.sha256", "evidence/approvals.json", "evidence/MANIFEST.sha256", "evidence/MANIFEST.sig")],
    "findings/F-*.yaml", "lineage/edges.jsonl", "audit/anchors/*.json",
]


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def _match(rel: str) -> tuple[str | None, str]:
    for pat, schema, kind in FILE_SCHEMAS:
        if fnmatch.fnmatch(rel, pat):
            return schema, kind
    raise AssertionError(f"no schema mapping for {rel}")


def _read(root: Path, rel: str, kind: str) -> list:
    p = root / rel
    if kind == "yaml":
        return [json.loads(json.dumps(io.read_yaml(p)))]
    if kind == "json":
        return [json.loads(p.read_text(encoding="utf-8"))]
    if kind == "jsonl":
        return io.read_jsonl(p)
    if kind == "manifest":
        return [_manifest(p)]
    return []


def _manifest(p: Path) -> dict:
    entries = []
    for line in p.read_text(encoding="utf-8").splitlines():
        digest, sep, path = line.partition("  ")
        assert sep, f"{p}: malformed line {line!r}"
        entries.append({"sha256": digest, "path": path})
    return {"entries": entries}


def _validator(name: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json_schema(name), format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)


# ---------------------------------------------------------------- schemas


@pytest.mark.parametrize("asset", list(ASSETS))
def test_fixture_has_the_full_tree(asset):
    files = _files(EXAMPLES / asset)
    for pat in TREE:
        assert any(fnmatch.fnmatch(f, pat) for f in files), f"{asset}: nothing matches {pat}"


@pytest.mark.parametrize("asset", list(ASSETS))
def test_every_fixture_file_validates(asset):
    root = EXAMPLES / asset
    seen = set()
    for rel in _files(root):
        schema, kind = _match(rel)
        if kind == "json-schema":
            jsonschema.Draft202012Validator.check_schema(json.loads((root / rel).read_text()))
        if kind == "pem":
            load_pem_public_key((root / rel).read_bytes())
        if kind == "cyclonedx":
            bom = json.loads((root / rel).read_text())
            assert bom["bomFormat"] == "CycloneDX" and bom["specVersion"] == "1.6"
        if schema is None:
            continue
        v = _validator(schema)
        for inst in _read(root, rel, kind):
            v.validate(inst)
            SCHEMA_MODELS[schema].model_validate(inst)
        seen.add(schema)
    file_schemas = {s for _, s, _ in FILE_SCHEMAS if s}
    assert seen == file_schemas, f"unused: {file_schemas - seen}"


@pytest.mark.parametrize("asset", list(ASSETS))
def test_outputs_match_task_schema(asset):
    root = EXAMPLES / asset
    v = jsonschema.Draft202012Validator(json.loads((root / "evals/output.schema.json").read_text()))
    for rel in _files(root):
        if rel.endswith(".jsonl") and ("predictions" in rel or "holdout" in rel or "evals/" in rel):
            for row in io.read_jsonl(root / rel):
                for k in ("output", "expected", "model_output"):
                    if k in row:
                        v.validate(row[k])


def test_conformance_cases():
    readme = (CONFORMANCE / "README.md").read_text()
    for name in SCHEMA_MODELS:
        v = _validator(name)
        valid = sorted((CONFORMANCE / "valid" / name).glob("*.json"))
        invalid = sorted((CONFORMANCE / "invalid" / name).glob("*.json"))
        assert valid and invalid, f"{name}: needs valid and invalid cases"
        for f in valid:
            v.validate(json.loads(f.read_text()))
        for f in invalid:
            inst = json.loads(f.read_text())
            assert not v.is_valid(inst), f"{f} should fail {name}.schema.json"
            with pytest.raises(ValueError):  # pydantic.ValidationError is a ValueError
                SCHEMA_MODELS[name].model_validate(inst)
            assert f"`invalid/{name}/{f.name}`" in readme, f"README does not explain {f}"


# ---------------------------------------------------------------- integrity


@pytest.mark.parametrize("asset", list(ASSETS))
def test_manifests_are_correct(asset):
    root = EXAMPLES / asset
    n = ASSETS[asset]
    for rel in (f"rebases/{n}/MANIFEST.sha256", f"rebases/{n}/evidence/MANIFEST.sha256"):
        entries = _manifest(root / rel)["entries"]
        assert [e["path"] for e in entries] == sorted(e["path"] for e in entries)
        for e in entries:
            assert sha256_file(root / e["path"]) == e["sha256"], f"{rel}: {e['path']}"
    listed = {e["path"] for e in _manifest(root / f"rebases/{n}/MANIFEST.sha256")["entries"]}
    rebase_files = {f"rebases/{n}/{f}" for f in _files(root / f"rebases/{n}")
                    if f != "MANIFEST.sha256" and not f.startswith("evidence/")}
    assert listed == rebase_files | {"data/holdout/H-2026-11-01/holdout.jsonl"}


@pytest.mark.parametrize("asset", list(ASSETS))
def test_lock_is_complete_and_sorted(asset):
    root = EXAMPLES / asset
    lock = json.loads((root / "roadbed.lock").read_text())
    paths = [f["path"] for f in lock["files"]]
    assert paths == sorted(paths, key=str.encode)
    assert paths == sorted((f for f in _files(root) if f != "roadbed.lock"), key=str.encode)
    for f in lock["files"]:
        assert sha256_file(root / f["path"]) == f["sha256"] and (root / f["path"]).stat().st_size == f["size"]
    oids = [o["oid"] for o in lock["objects"]]
    assert oids == sorted(oids)
    for o in lock["objects"]:
        assert o["oid"] in (root / o["ref"]).read_text(), f"{o['ref']} does not name {o['oid']}"


def _verify_sig(root: Path, sig_rel: str) -> dict:
    sig = json.loads((root / sig_rel).read_text())
    key = load_pem_public_key((root / sig["key"]).read_bytes())
    data = (root / sig["signed"]).read_bytes()
    msg = rfc8785.dumps(json.loads(data)) if sig["payload"] == "jcs" else data
    key.verify(base64.b64decode(sig["sig"]), msg)  # raises InvalidSignature
    assert sig["keyid"] == ref.sha256_hex(key.public_bytes_raw())
    return sig


@pytest.mark.parametrize("asset", list(ASSETS))
def test_signatures_verify(asset):
    root, n = EXAMPLES / asset, ASSETS[asset]
    assert _verify_sig(root, "versions/v12/receipt.sig")["signed"] == "versions/v12/receipt.json"
    assert _verify_sig(root, f"rebases/{n}/receipt.sig")["signed"] == f"rebases/{n}/receipt.json"
    assert _verify_sig(root, f"rebases/{n}/evidence/MANIFEST.sig")["key"] == "policy/keys/evidence.pub"


@pytest.mark.parametrize("asset", list(ASSETS))
def test_tampered_receipt_fails_signature(asset, tmp_path):
    from cryptography.exceptions import InvalidSignature

    root = tmp_path / asset
    shutil.copytree(EXAMPLES / asset, root)
    p = root / f"rebases/{ASSETS[asset]}/receipt.json"
    r = json.loads(p.read_text())
    r["predicate"]["seed"] += 1
    p.write_text(json.dumps(r, indent=2))
    with pytest.raises(InvalidSignature):
        _verify_sig(root, f"rebases/{ASSETS[asset]}/receipt.sig")


@pytest.mark.parametrize("asset", list(ASSETS))
def test_receipt_chain_and_seal_order(asset):
    root, n = EXAMPLES / asset, ASSETS[asset]
    v12 = json.loads((root / "versions/v12/receipt.json").read_text())
    rec = json.loads((root / f"rebases/{n}/receipt.json").read_text())
    seal = json.loads((root / "data/holdout/H-2026-11-01/SEAL.json").read_text())
    meta = io.read_yaml(root / f"rebases/{n}/rebase.yaml")
    p = rec["predicate"]
    assert p["prev_receipt"] == sha256_canonical(v12)
    assert v12["predicate"]["prev_receipt"] is None
    assert {p["executor"]["name"], v12["predicate"]["executor"]["name"]} == {"mock"}
    # seal precedes training: audit seq, timestamp, and the recorded git ancestry
    assert seal["audit_seq"] == p["holdout"]["seal_seq"] < p["audit_seq"]
    assert seal["sealed_at"] < meta["created_at"] < p["started_at"] < p["ended_at"]
    c = json.loads((root / f"rebases/{n}/contamination.json").read_text())
    assert c["seal_order"]["ok"] and c["seal_order"]["git_ancestor"] is True
    assert c["seal_order"]["train_seq"] == p["audit_seq"]


@pytest.mark.parametrize("asset", list(ASSETS))
def test_dataset_roots_and_disjointness(asset):
    root, n = EXAMPLES / asset, ASSETS[asset]
    hold = io.read_jsonl(root / "data/holdout/H-2026-11-01/holdout.jsonl")
    snap = io.read_jsonl(root / "data/snapshots/2026-11-01/part-0000.jsonl")
    for r in snap:
        assert ref.row_hash(r) == r["row_hash"]
    seal = json.loads((root / "data/holdout/H-2026-11-01/SEAL.json").read_text())
    man = json.loads((root / "data/snapshots/2026-11-01/MANIFEST.json").read_text())
    rec = json.loads((root / f"rebases/{n}/receipt.json").read_text())["predicate"]
    h_root = ref.merkle_root([r["row_hash"] for r in hold])
    s_root = ref.merkle_root([r["row_hash"] for r in snap])
    assert seal["merkle_root"] == rec["holdout"]["merkle_root"] == h_root
    assert man["merkle_root"] == rec["snapshot"]["merkle_root"] == s_root
    assert seal["sha256"] == sha256_file(root / "data/holdout/H-2026-11-01/holdout.jsonl")
    assert seal["rows"] == len(hold) and man["rows"] == len(snap)
    assert not {r["row_hash"] for r in hold} & {r["row_hash"] for r in snap}
    assert man["parts"][0]["oid"] == sha256_file(root / man["parts"][0]["path"])


def test_blocked_has_finding_and_pass_awaits_signoff():
    gate = json.loads((EXAMPLES / "blocked/rebases/14/gate.json").read_text())
    assert gate["status"] == "blocked" and [f["id"] for f in gate["findings"]] == ["F-0001"]
    assert io.read_yaml(EXAMPLES / "blocked/findings/F-0001.yaml")["slice"] == "line=bodily_injury"
    appr = json.loads((EXAMPLES / "pass/rebases/13/evidence/approvals.json").read_text())
    assert appr["status_text"] == "awaiting MRM sign-off"
    assert json.loads((EXAMPLES / "pass/rebases/13/gate.json").read_text())["status"] == "pass"


# ---------------------------------------------------------------- vectors


def test_hash_vectors():
    v = json.loads((CONFORMANCE / "vectors/hashing.json").read_text())
    for c in v["jcs"]:
        assert rfc8785.dumps(c["object"]).decode() == c["jcs"] and sha256_canonical(c["object"]) == c["sha256"]
    for c in v["row_hash"]:
        assert ref.row_hash(c["row"]) == c["sha256"]
    assert [len(c["leaves"]) for c in v["merkle"]] == [0, 1, 2, 3, 5]
    for c in v["merkle"]:
        assert ref.merkle_root(c["leaves"]) == c["root"]
    assert v["merkle"][0]["root"] == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_audit_chain_vector():
    events = io.read_jsonl(CONFORMANCE / "vectors/audit-chain.jsonl")
    prev = "0" * 64
    v = _validator("audit-event")
    for e in events:
        v.validate(e)
        assert e["prev_hash"] == prev
        assert sha256_canonical({k: x for k, x in e.items() if k != "hash"}) == e["hash"]
        prev = e["hash"]


# ---------------------------------------------------------------- determinism and verify


def test_examples_are_up_to_date(tmp_path):
    ref.main(["--out", str(tmp_path)])
    for asset in ASSETS:
        assert _files(tmp_path / "examples" / asset) == _files(EXAMPLES / asset)
        for f in _files(EXAMPLES / asset):
            assert (tmp_path / "examples" / asset / f).read_bytes() == (EXAMPLES / asset / f).read_bytes(), (
                f"{asset}/{f} is stale: run `uv run python spec/tools/build_examples.py`"
            )
    for f in _files(CONFORMANCE / "vectors"):
        assert (tmp_path / "vectors" / f).read_bytes() == (CONFORMANCE / "vectors" / f).read_bytes()


@pytest.mark.parametrize("asset", list(ASSETS))
def test_roadbed_verify_passes(asset, tmp_path, monkeypatch):
    root = tmp_path / asset
    shutil.copytree(EXAMPLES / asset, root)
    monkeypatch.chdir(root)
    r = runner.invoke(app, ["verify", f"rebases/{ASSETS[asset]}"])
    assert r.exit_code == 0, r.output  # a blocked rebase still verifies: verify checks integrity, not the gate


@pytest.mark.parametrize("asset", list(ASSETS))
def test_roadbed_verify_exits_4_on_tamper(asset, tmp_path):
    root = tmp_path / asset
    shutil.copytree(EXAMPLES / asset, root)
    f = root / f"rebases/{ASSETS[asset]}/predictions.candidate.jsonl"
    f.write_bytes(f.read_bytes().replace(b'"severity":1', b'"severity":2', 1))
    r = runner.invoke(app, ["verify", str(root / f"rebases/{ASSETS[asset]}")])
    assert r.exit_code == 4, r.output
    assert "V-HASH-MISMATCH" in r.output


# ---------------------------------------------------------------- Executor protocol


def test_executor_protocol_shape():
    class Dummy:
        name = "mock"

        def submit(self, job: ExecutorJob) -> str:
            return job.id

        def status(self, job_id: str) -> JobStatus: ...

        def result(self, job_id: str) -> JobResult: ...

        def cancel(self, job_id: str) -> None: ...

    class Missing:
        name = "mock"

        def submit(self, job: ExecutorJob) -> str: ...

    assert isinstance(Dummy(), Executor)
    assert not isinstance(Missing(), Executor)
