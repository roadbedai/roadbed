"""Build spec/conformance/{valid,invalid}/<schema>/*.json and spec/conformance/README.md. Deterministic.

    uv run python spec/tools/build_conformance.py

Valid cases come from spec/examples/pass (run build_examples.py first) plus hand-written API bodies.
Each invalid case is a valid case with one change, and the README says which rule it breaks.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from roadbed.io import read_jsonl, read_yaml

SPEC_DIR = Path(__file__).resolve().parents[1]
PASS = SPEC_DIR / "examples" / "pass"
OUT = SPEC_DIR / "conformance"
H = "a" * 64
TS = "2026-11-01T10:20:00Z"


def _yaml(rel: str) -> Any:
    return json.loads(json.dumps(read_yaml(PASS / rel)))


def _json(rel: str) -> Any:
    return json.loads((PASS / rel).read_text(encoding="utf-8"))


def _row(rel: str) -> Any:
    return read_jsonl(PASS / rel)[0]


def _manifest(rel: str) -> dict[str, Any]:
    lines = (PASS / rel).read_text(encoding="utf-8").splitlines()
    return {"entries": [{"sha256": ln[:64], "path": ln[66:]} for ln in lines if ln.strip()]}


def _job(receipt: dict[str, Any]) -> dict[str, Any]:
    """A self-contained train job built from the pass fixture: every input by oid, outputs named."""
    p = receipt["predicate"]
    man_bytes = (PASS / "data/snapshots/2026-11-01/MANIFEST.json").read_bytes()
    man = json.loads(man_bytes)
    hold = read_jsonl(PASS / "data/holdout/H-2026-11-01/holdout.jsonl")
    hashes = "".join(h + "\n" for h in sorted(r["row_hash"] for r in hold)).encode()
    seal = _json("data/holdout/H-2026-11-01/SEAL.json")

    return {
        "id": "job-rebase-13-train", "model": "claims-triage", "rebase": 13,
        "spec": {
            "kind": "train", "base": p["base"], "recipe": p["recipe"],
            "snapshot": {**p["snapshot"], "manifest": {"oid": hashlib.sha256(man_bytes).hexdigest(), "size": len(man_bytes)},
                         "parts": [{"oid": x["oid"], "size": x["size"]} for x in man["parts"]]},
            "holdout": {**p["holdout"], "row_hashes": {"oid": hashlib.sha256(hashes).hexdigest(), "size": len(hashes)},
                        "minhash": {"oid": seal["minhash_oid"], "size": 1}},
            "seed": 20261101, "image_digest": p["image_digest"], "adapter_name": "claims-triage-v13",
            "outputs": ["adapter", "receipt"],
        },
        "storage": {"endpoint": "http://minio:9000", "bucket": "acme-claims", "credentials": "runner-bucket-rw"},
        "cost_cap_eur": 25.0, "created_at": TS, "requested_by": "dana", "receipt_path": "rebases/13/receipt.json",
    }


def _eval_job(receipt: dict[str, Any]) -> dict[str, Any]:
    """The eval job of rebase 13: holdout rows by oid (runner exemption), plus the snapshot the receipt copies."""
    job = _job(receipt)
    p, train = receipt["predicate"], job["spec"]
    rows = (PASS / "data/holdout/H-2026-11-01/holdout.jsonl").read_bytes()
    out_schema = (PASS / "evals/output.schema.json").read_bytes()
    job["id"], job["spec"] = "job-rebase-13-eval-candidate", {
        "kind": "eval", "base": p["base"], "recipe": p["recipe"], "adapter": {"oid": p_adapter(receipt), "size": 66060288},
        "role": "candidate",
        "holdout": {**train["holdout"], "file": {"oid": hashlib.sha256(rows).hexdigest(), "size": len(rows)}},
        "snapshot": p["snapshot"],
        "output_schema": {"oid": hashlib.sha256(out_schema).hexdigest(), "size": len(out_schema)},
        "max_tokens": 64, "temperature": 0.0, "outputs": ["predictions", "receipt"],
    }
    return job


def _bench_job(receipt: dict[str, Any]) -> dict[str, Any]:
    """The bench job of rebase 13: no holdout rows; snapshot and sealed holdout refs only, for the receipt."""
    job = _job(receipt)
    p = receipt["predicate"]
    job["id"], job["spec"] = "job-rebase-13-bench", {
        "kind": "bench", "base": p["base"], "recipe": p["recipe"], "adapter": {"oid": p_adapter(receipt), "size": 66060288},
        "prompts": {"oid": H, "size": 512000}, "count": 500, "concurrency": 32, "max_tokens": 64,
        "price_per_hour_eur": 2.5, "snapshot": p["snapshot"], "holdout": p["holdout"], "outputs": ["bench", "receipt"],
    }
    return job


def p_adapter(receipt: dict[str, Any]) -> str:
    return str(receipt["subject"][0]["digest"]["sha256"])


def valid_cases() -> dict[str, Any]:
    receipt = _json("rebases/13/receipt.json")
    sig = _json("rebases/13/receipt.sig")
    return {
        "roadbed": _yaml("roadbed.yaml"),
        "holdout-row": _row("data/holdout/H-2026-11-01/holdout.jsonl"),
        "prediction-row": _row("rebases/13/predictions.candidate.jsonl"),
        "scorers": _yaml("evals/scorers.yaml"),
        "slices": _yaml("evals/slices.yaml"),
        "gate-policy": _yaml("policy/gate.yaml"),
        "license-policy": _yaml("policy/license.yaml"),
        "scores": _json("rebases/13/scores.json"),
        "gate": _json("rebases/13/gate.json"),
        "lock": _json("roadbed.lock"),
        "object-pointer": {"spec": "roadbed/v0.1", "oid": H, "size": 66060288, "media_type": "application/octet-stream"},
        "snapshot-manifest": _json("data/snapshots/2026-11-01/MANIFEST.json"),
        "snapshot-row": _row("data/snapshots/2026-11-01/part-0000.jsonl"),
        "seal": _json("data/holdout/H-2026-11-01/SEAL.json"),
        "tombstone-row": _row("data/tombstones.jsonl"),
        "golden-row": _row("evals/golden.jsonl"),
        "correction-test-row": _row("evals/correction_tests.jsonl"),
        "recipe": _yaml("recipes/qwen3/lora-r16-sft@v4.yaml"),
        "model-version": _json("versions/v12/model.json"),
        "receipt": receipt,
        "signature": sig,
        "rebase": _yaml("rebases/13/rebase.yaml"),
        "contamination": _json("rebases/13/contamination.json"),
        "cost": _json("rebases/13/cost.json"),
        "license-decision": _json("rebases/13/license.json"),
        "approvals": _json("rebases/13/evidence/approvals.json"),
        "finding": _yaml("findings/F-0001.yaml"),
        "lineage-edge": _row("lineage/edges.jsonl"),
        "audit-anchor": _json("audit/anchors/2026-11-01.json"),
        "evidence-manifest": _manifest("rebases/13/evidence/MANIFEST.sha256"),
        "audit-event": {
            "seq": 1377, "ts": TS, "actor": "dana", "role": "developer", "action": "rebase.approve",
            "object": {"type": "rebase", "id": "13"}, "outcome": "denied", "rule": "SoD-02",
            "payload": {"rebase": 13}, "prev_hash": H, "hash": "b" * 64,
        },
        "approval": {"rebase": 13, "actor": "vik", **_json("rebases/13/evidence/approvals.json")["approvals"]["vik"]},
        "executor-job": _job(receipt),
        "job-status": {"job_id": "job-rebase-13-train", "state": "running", "progress": 0.4,
                       "message": "epoch 2 of 3", "updated_at": TS, "lease_id": "lease-7f3a", "attempt": 1},
        "job-lease": {"job": _job(receipt), "lease_id": "lease-7f3a", "attempt": 1, "max_attempts": 3,
                      "lease_until": "2026-11-01T10:25:00Z", "audit_seq": receipt["predicate"]["audit_seq"],
                      "prev_receipt": receipt["predicate"]["prev_receipt"]},
        "lease-state": {"lease_until": "2026-11-01T10:30:00Z", "cancel_requested": False},
        "heartbeat": {"lease_id": "lease-7f3a", "attempt": 1},
        "job-result": {
            "job_id": "job-rebase-13-train", "state": "succeeded",
            "artifacts": [{"name": "adapter", "oid": receipt["subject"][0]["digest"]["sha256"], "size": 66060288}],
            "receipt": receipt, "receipt_sig": sig, "metrics": {"train_loss": 0.41}, "error": None,
            "finished_at": TS, "lease_id": "lease-7f3a", "attempt": 1,
        },
        "alias-config": {
            "spec": "roadbed/v0.1", "generation": 7,
            "aliases": {"claims-triage-prod": {
                "upstream": "http://vllm:8000/v1", "adapter": "claims-triage-v12", "version": "v12",
                "canary": {"weight": 0.1, "salt": "c13", "adapter": "claims-triage-v13", "version": "v13"},
                "shadow": None}},
        },
        "interaction-in": {
            "interaction_id": "int-7f3a-0001", "model": "claims-triage", "alias": "claims-triage-prod",
            "version": "v12", "ts": TS, "input_ref": "sha256:" + H,
            "input": {"narrative": "<PERSON_1> reported a rear-end collision on <DATE_1>.", "claimant": "tok_7f3a91c2"},
            "output": {"route": "standard", "severity": 2}, "profile_id": "insurer-default@v1",
            "hmac_key_id": "acme-hmac-2026-10", "subject_token": "tok_7f3a91c2",
            "slices": {"line": "bodily_injury", "state": "CA", "channel": "direct"}, "latency_ms": 412.0,
            "synthetic": True,
        },
        "verdict-in": {"verdict_id": "vd-7f3a-0001", "interaction_id": "int-7f3a-0001", "action": "fix", "verdict": {"route": "standard", "severity": 3},
                       "reason_code": "severity_low", "ts": TS, "synthetic": True},
        "outcome-in": {"outcome_id": "oc-7f3a-0001", "interaction_id": "int-7f3a-0001", "outcome": {"paid": True, "litigated": False},
                       "source": "system_of_record", "observed_at": "2026-12-01T00:00:00Z", "synthetic": True},
    }


Mut = Callable[[dict[str, Any]], None]


def extra_valid_cases() -> dict[str, list[tuple[str, Any, str]]]:
    """More valid cases per schema: (case, instance, what it shows). W01b additions."""
    receipt = _json("rebases/13/receipt.json")
    bench = copy.deepcopy(receipt)
    bench["predicate"]["job"] = {"id": "job-rebase-13-bench", "kind": "bench"}
    del bench["predicate"]["snapshot"], bench["predicate"]["holdout"]
    first = _json("versions/v12/receipt.json")
    return {
        "executor-job": [("eval-with-snapshot", _eval_job(receipt), "an eval job carries the snapshot ref its receipt copies"),
                         ("bench-with-refs", _bench_job(receipt), "a bench job may carry snapshot and sealed holdout refs")],
        "receipt": [("bench-without-data", bench, "a bench receipt may leave out snapshot and holdout")],
        "job-lease": [("first-receipt", {"job": {**_job(first), "receipt_path": "versions/v12/receipt.json"},
                                         "lease_id": "lease-0001", "attempt": 1, "max_attempts": 3,
                                         "lease_until": "2026-10-20T08:05:00Z", "audit_seq": 900, "prev_receipt": None},
                       "the first receipt of an asset has prev_receipt null")],
    }


def _del(*path: str) -> Mut:
    def f(o: dict[str, Any]) -> None:
        for k in path[:-1]:
            o = o[k]
        del o[path[-1]]
    return f


def _all(*muts: Mut) -> Mut:
    def f(o: dict[str, Any]) -> None:
        for m in muts:
            m(o)
    return f


def _set(value: Any, *path: str | int) -> Mut:
    def f(o: Any) -> None:
        for k in path[:-1]:
            o = o[k]
        o[path[-1]] = value
    return f


SYN_STR = ("synthetic-not-bool", _set("maybe", "synthetic"), "synthetic is a boolean (W01b)")


# schema -> [(case, mutation, expected failure)]
INVALID: dict[str, list[tuple[str, Mut, str]]] = {
    "roadbed": [("revision-branch-name", _set({"repo": "Qwen/Qwen3.8-4B", "revision": "main"}, "base"),
                 "base.revision must be 40 hex characters or 'unpinned'")],
    "holdout-row": [("input-ref-no-prefix", _set(H, "input_ref"), "input_ref must be sha256:<64 hex>"), SYN_STR],
    "prediction-row": [("missing-output", _del("output"), "output is required")],
    "scorers": [("zero-weight", _set([{"field": "route", "weight": 0}], "fields"), "weight must be > 0")],
    "slices": [("missing-keys", _del("keys"), "keys is required")],
    "gate-policy": [("ci-above-one", _set(1.5, "ci"), "ci must be in (0, 1)")],
    "license-policy": [("bad-default", _set("allow", "default"), "default must be deny or review")],
    "scores": [("bad-holdout-digest", _set("XYZ", "holdout", "sha256"), "holdout.sha256 must be 64 lowercase hex")],
    "gate": [("unknown-check-name", _set("vibes", "checks", 0, "name"), "check names are one vocabulary (SPEC.md, Gate names)"),
             ("unknown-status", _set("warn", "status"), "status must be pass or blocked"),
             ("pass-with-failed-check", _set("fail", "checks", 2, "status"), "a passing gate has no failed check"),
             ("skipped-without-reason", _set("", "checks", 2, "detail"), "every check, skipped or not, says why")],
    "lock": [("negative-size", _set(-1, "files", 0, "size"), "files[].size must be >= 0")],
    "object-pointer": [("small-file", _set(512, "size"), "pointers exist only for files over 1 MB")],
    "snapshot-manifest": [("missing-pseudonymisation", _del("pseudonymisation"),
                           "pseudonymisation (profile, hmac_key_id) is required")],
    "snapshot-row": [("ts-milliseconds", _set("2026-10-02T09:01:00.120Z", "ts"),
                      "timestamps are YYYY-MM-DDTHH:MM:SSZ: no fraction (hashed fields keep stored bytes)"),
                     ("ts-offset", _set("2026-10-02T09:01:00+00:00", "ts"), "timestamps end in Z, not an offset"),
                     ("missing-row-hash", _del("row_hash"), "row_hash is required on snapshot rows"), SYN_STR],
    "seal": [("bad-id", _set("holdout-2026", "id"), "holdout ids are H-..."),
             ("probe-w01-fields-partial", _all(_del("merkle_root"), _del("minhash_oid")),
              "merkle_root, minhash_oid, snapshot and stratification come together (verifier probe)"),
             ("probe-sealed-by-email", _set("jane.doe@acme.example", "sealed_by"),
              "sealed_by is a user id, never an email address (verifier probe)"),
             ("missing-audit-seq", _del("audit_seq"), "audit_seq is required (seal ordering)"),
             ("short-commit", _set("5e41c0a", "seal_commit"), "seal_commit must be a 40-hex commit")],
    "tombstone-row": [("raw-email-subject", _set("jane@example.com", "subject_token"),
                       "subject_token must be an HMAC token tok_<hex>, never a raw identifier")],
    "golden-row": [("extra-field", _set("free text", "narrative"), "unknown fields are rejected"), SYN_STR],
    "correction-test-row": [("bad-id", _set("CT-97", "id"), "id must match CT-NNNN"), SYN_STR],
    "recipe": [("full-finetune", _set("full", "method"), "method must be lora or qlora")],
    "model-version": [("version-name", _set("latest", "version"), "version must match vN")],
    "receipt": [("probe-mock-with-gpu-and-cost", _all(
                    _set({"name": "NVIDIA H100 80GB HBM3", "count": 1}, "predicate", "gpu"),
                    _set({"gpu_hours": 0.3, "price_per_hour_eur": 2.5, "eur": 0.75}, "predicate", "cost"),
                    _set(None, "predicate", "synthetic")),
                 "a mock or replay receipt has gpu 'none' and zero cost (verifier probe)"),
                ("missing-seal-commit", _del("predicate", "holdout", "seal_commit"),
                 "predicate.holdout carries seal_commit and sealed_at"),
                ("missing-executor", _del("predicate", "executor"), "predicate.executor is required"),
                ("wrong-predicate-type", _set("https://slsa.dev/provenance/v1", "predicateType"),
                 "predicateType must be https://roadbed.dev/run/v1"),
                ("unknown-executor", _set({"name": "laptop", "version": "1"}, "predicate", "executor"),
                 "executor.name must be runner, mock, replay, hf-jobs or together"),
                ("rebase-without-snapshot", _del("predicate", "snapshot"),
                 "only a bench receipt may leave out snapshot (W01b)"),
                ("eval-without-holdout", _all(_set({"id": "job-rebase-13-eval", "kind": "eval"}, "predicate", "job"),
                                              _del("predicate", "holdout")),
                 "an eval receipt names its sealed holdout (W01b)")],
    "signature": [("rsa", _set("rsa", "alg"), "alg must be ed25519")],
    "rebase": [("unknown-state", _set("merged", "state"), "state must be a rebase state of the orchestrator")],
    "contamination": [
        ("missing-seal-order", _del("seal_order"), "seal_order is required"),
        ("probe-pass-with-shared-rows", _all(_set(3, "exact", "shared"), _set(None, "seal_order", "git_ancestor"),
                                             _set(None, "seal_order", "seal_commit")),
         "status pass needs shared 0; git_ancestor is a required boolean; the seal commit comes from git (verifier probe)"),
        ("probe-self-declared-seq", _all(_set(2000, "seal_order", "seal_seq"), _set(10, "seal_order", "train_seq")),
         "seal_order holds no copied numbers; verify reads them from SEAL.json and the receipt (verifier probe)"),
        ("ok-with-false-leg", _set(False, "seal_order", "git_ancestor"), "ok true needs all three legs true"),
    ],
    "cost": [("negative-total", _set(-1.0, "total_eur"), "total_eur must be >= 0"),
             ("probe-mock-with-h100", _set({"name": "NVIDIA H100 80GB HBM3", "count": 1}, "gpu"),
              "a mock or replay cost.json has gpu 'none' and zero cost (verifier probe)")],
    "license-decision": [("decision-maybe", _set("maybe", "decision"), "decision must be allow, review or deny")],
    "approvals": [("probe-comment-free-text", _set("Called claimant Jane Doe at +358401234567", "approvals", "vik", "comment"),
                   "no free-text comment: reason_code and refs only (verifier probe)"),
                  ("probe-same-actor-two-roles", lambda o: o.update(approvals=[
                      {"actor": "vik", **o["approvals"]["vik"]},
                      {"actor": "vik", **o["approvals"]["vik"], "role": "approver", "decision": "approved"}],
                      status="approved"),
                   "approvals is keyed by actor id, so one actor cannot be validator and approver (SoD-03, verifier probe)"),
                  ("email-actor-key", lambda o: o["approvals"].update({"jane.doe@acme.example": o["approvals"].pop("vik")}),
                   "approval keys are user ids, never email addresses"),
                  ("validator-approves", _set("approved", "approvals", "vik", "decision"),
                   "a validator records validated, validated_with_findings or rejected (review probe)"),
                  ("model-history-disagrees", _set(999, "history", 0, "audit_seq"),
                   "approvals holds each actor's latest decision in history (model only: JSON Schema cannot compare two fields)"),
                  ("self-approve-role", _set("developer", "approvals", "vik", "role"),
                   "approvals[].role must be validator or approver")],
    "finding": [("old-kind-license", _set("license", "kind"), "kind is the failure reason: license_denied"),
                ("bad-id", _set("F-3", "id"), "id must match F-NNNN")],
    "lineage-edge": [("untyped-node", _set("v12", "from"), "from must be <type>:<id>"),
                     ("probe-person-name-actor", _set("Jane Doe", "actor"), "actor is a user id (verifier probe)"),
                     ("probe-raw-email-subject", _set("subject:jane.doe@acme.example", "to"),
                      "a subject node is subject:tok_<hex> (verifier probe)")],
    "audit-anchor": [("missing-head-hash", _del("head_hash"), "head_hash is required")],
    "evidence-manifest": [("empty", _set([], "entries"), "a MANIFEST has at least one entry")],
    "audit-event": [("probe-email-actor-free-text", _all(_set("jane.doe@acme.example", "actor"),
                                                         _set({"note": "claimant Jane Doe 040-1234567"}, "payload")),
                     "actor is a user id; payload strings have no spaces and no free-text keys (verifier probe)"),
                    ("probe-denied-without-rule", _set(None, "rule"), "a denied event names its rule (verifier probe)"),
                    ("free-text-key", _set({"comment": "ok"}, "payload"), "free-text payload keys are rejected"),
                    ("free-text-payload", _set({"note": {"text": "call me"}}, "payload"),
                    "payload values must be scalars (ids, hashes, aggregates)"),
                    ("unknown-rule", _set("SoD-09", "rule"), "rule must be SoD-01 to SoD-05")],
    "approval": [("unknown-decision", _set("lgtm", "decision"), "decision is an enum"),
                 ("validator-approves", _set("approved", "decision"),
                  "a validator records validated, validated_with_findings or rejected (review probe)"),
                 ("approver-validates", _all(_set("approver", "role"), _set("validated", "decision")),
                  "an approver records approved or rejected (review probe)"),
                 ("free-text-comment", _set("looks fine to me", "comment"), "no free-text comment"),
                 ("bad-ref", _set(["call Jane"], "refs"), "refs are finding, correction-test, audit or rebase ids")],
    "executor-job": [("unknown-kind", _set("finetune", "spec", "kind"), "spec.kind must be train, eval or bench"),
                     ("no-parts", _set([], "spec", "snapshot", "parts"), "the runner needs the snapshot part oids"),
                     ("no-storage", _del("storage"), "the job names its bucket"),
                     ("secret-in-credentials", _set("AKIA/secret+key=", "storage", "credentials"),
                      "credentials is the name of a credential, never the secret"),
                     ("receipt-path-outside", _set("rebases/13/../../policy/receipt.json", "receipt_path"),
                      "receipt_path is versions/vN/receipt.json or rebases/N/receipt.json (W01b)"),
                     ("receipt-path-sig", _set("rebases/13/receipt.sig", "receipt_path"),
                      "receipt_path names the receipt, not its signature (W01b)")],
    "job-lease": [("no-attempt", _del("attempt"), "a lease names its attempt"),
                  ("audit-seq-zero", _set(0, "audit_seq"), "audit_seq starts at 1 (W01b)"),
                  ("prev-receipt-prefixed", _set("sha256:" + H, "prev_receipt"),
                   "prev_receipt is bare lowercase hex (W01b)")],
    "lease-state": [("no-cancel-flag", _del("cancel_requested"), "the runner learns about cancels here")],
    "heartbeat": [("extra-field", _set("x", "note"), "request bodies reject unknown fields")],
    "job-status": [("progress-over-one", _set(1.5, "progress"), "progress must be in [0, 1]")],
    "job-result": [("succeeded-without-receipt", _del("receipt"), "receipt is required when state is succeeded"),
                   ("probe-succeeded-sig-null", _set(None, "receipt_sig"),
                    "receipt_sig is required when state is succeeded (verifier probe)"),
                   ("probe-succeeded-sig-absent", _del("receipt_sig"),
                    "receipt_sig is required when state is succeeded (verifier probe)")],
    "alias-config": [("canary-weight", _set(2.0, "aliases", "claims-triage-prod", "canary", "weight"),
                      "canary.weight must be in [0, 1]")],
    "interaction-in": [("no-profile", _del("profile_id"), "profile_id is required: ingest without it is a 422")],
    "verdict-in": [("fix-without-verdict", _del("verdict"), "a fix must carry verdict and reason_code"),
                   ("unknown-action", _set("delete", "action"), "action must be approve, fix, reject or escalate"), SYN_STR],
    "outcome-in": [("bad-source", _set("email", "source"), "source must be system_of_record or csv"), SYN_STR],
}


def write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    valid = valid_cases()
    for sub in ("valid", "invalid"):
        if (OUT / sub).exists():
            shutil.rmtree(OUT / sub)
    rows = []
    for name, inst in valid.items():
        write_json(OUT / "valid" / name / "example.json", inst)
        for case, mut, why in INVALID[name]:
            bad = copy.deepcopy(inst)
            mut(bad)
            write_json(OUT / "invalid" / name / f"{case}.json", bad)
            rows.append(f"| `invalid/{name}/{case}.json` | `{name}.schema.json` | {why} |")
    extra_rows = []
    for name, cases in extra_valid_cases().items():
        for case, inst, why in cases:
            write_json(OUT / "valid" / name / f"{case}.json", inst)
            extra_rows.append(f"| `valid/{name}/{case}.json` | `{name}.schema.json` | {why} |")
    readme = [
        "# Conformance cases",
        "",
        "Generated by `spec/tools/build_conformance.py`. Do not edit by hand.",
        "",
        "Each file in `valid/<schema>/` must validate against `schemas/v0.1/<schema>.schema.json`.",
        "Each file in `invalid/<schema>/` must fail validation against that schema, for the reason in the table.",
        "Each invalid case is the valid case with one change.",
        "A case named `model-*` breaks a rule that compares two fields. JSON Schema cannot express it, so only the model rejects it.",
        "A case whose only fault is an unknown field fails the published (closed) schema; models of files and responses ignore it (SPEC.md, compatibility).",
        "`vectors/` holds hashing, Merkle, audit-chain and ed25519 test vectors made by `spec/tools/build_examples.py`.",
        "",
        "| File | Schema | Expected failure |",
        "|---|---|---|",
        *rows,
        "",
        "More valid cases (besides `valid/<schema>/example.json`):",
        "",
        "| File | Schema | What it shows |",
        "|---|---|---|",
        *extra_rows,
        "",
    ]
    (OUT / "README.md").write_text("\n".join(readme), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
