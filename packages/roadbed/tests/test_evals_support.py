"""Shared helpers for the W14 tests (labels and evals). No tests here."""

from __future__ import annotations

import gzip
import json
from functools import cache
from pathlib import Path
from typing import Any

import rfc8785

from roadbed.io import read_yaml

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "acme-2k"
SCHEMAS = Path(__file__).resolve().parents[3] / "spec" / "schemas" / "v0.1"
AS_OF = "2026-11-01T09:00:00Z"
SEED = 7


@cache
def _events(name: str) -> tuple[str, ...]:
    with gzip.open(FIXTURE / f"{name}.jsonl.gz", "rt", encoding="utf-8") as f:
        return tuple(x for x in f if x.strip())


def events(name: str) -> list[dict[str, Any]]:
    """Fresh dicts each call, so a test can change them."""
    return [json.loads(x) for x in _events(name)]


def slices() -> dict[str, Any]:
    return read_yaml(FIXTURE / "slices.yaml")


def output_schema() -> dict[str, Any]:
    return json.loads((FIXTURE / "output.schema.json").read_text(encoding="utf-8"))


def published_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def input_ref(obj: dict[str, Any]) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(rfc8785.dumps(obj)).hexdigest()


def interaction(iid: str, *, ts: str = "2026-10-01T10:00:00Z", output: dict | None = None, narrative: str | None = None,
                line: str = "property", state: str = "CA", channel: str = "direct", synthetic: bool = True, **kw: Any) -> dict:
    inp = {"narrative": narrative or f"claim {iid}: a pipe burst in the kitchen, water damage to the floor", "line": line,
           "state": state, "channel": channel}
    row = {
        "interaction_id": iid, "model": "claims-triage", "version": "v12", "ts": ts, "input_ref": input_ref(inp), "input": inp,
        "output": output or {"route": "standard", "severity": 2}, "profile_id": "insurer-default@v1",
        "hmac_key_id": "synthetic-seed-7", "slices": {"line": line, "state": state, "channel": channel}, "synthetic": synthetic,
    }
    row.update(kw)
    return row


def verdict(iid: str, action: str, *, ts: str = "2026-10-02T10:00:00Z", vid: str | None = None, fix: dict | None = None,
            reason: str | None = None, **kw: Any) -> dict:
    v: dict[str, Any] = {"verdict_id": vid or f"vd-{iid}-{ts}", "interaction_id": iid, "action": action, "ts": ts}
    if action == "fix":
        v["verdict"] = fix or {"route": "complex", "severity": 3}
        v["reason_code"] = reason or "route_wrong"
    elif reason:
        v["reason_code"] = reason
    v.update(kw)
    return v


def outcome(iid: str, body: dict, *, observed_at: str = "2026-10-20T10:00:00Z", oid: str | None = None) -> dict:
    return {"outcome_id": oid or f"oc-{iid}-{observed_at}", "interaction_id": iid, "outcome": body, "source": "system_of_record",
            "observed_at": observed_at}


def fresh(n: int, tag: str) -> tuple[list[dict], list[dict]]:
    """n new approved interactions with texts that share no shingles with anything else: new labelled data."""
    lines, states, channels = ("property", "auto_physical", "liability"), ("CA", "TX", "FL"), ("direct", "broker", "partner")
    ints = [interaction(f"new-{tag}-{k:04d}", narrative=f"new {tag} claim {k} " + " ".join(f"{tag}{k}x{j}" for j in range(12)),
                        line=lines[k % 3], state=states[k % 3], channel=channels[(k // 3) % 3]) for k in range(n)]
    return ints, [verdict(i["interaction_id"], "approve") for i in ints]
