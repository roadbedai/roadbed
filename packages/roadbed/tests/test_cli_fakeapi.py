"""A fake control plane for the CLI tests (respx), built only from the spec fixtures, and its contract tests.

The fake is the server side of the test; the CLI is the subject. Every body it serves is a spec fixture file
(spec/examples/{pass,blocked}: SYNTHETIC data, receipts with executor mock) or the minimum API wrapper the contract
asks for. test_fake_bodies_match_openapi validates each body against a copy of platform/contracts/openapi.yaml
(cli_fixtures/openapi.yaml, generated from the same spec schemas: test_openapi_copy_matches_spec checks the hash).
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import pytest
import rfc8785
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from roadbed.io import read_yaml

TESTS = Path(__file__).resolve().parent
SPEC = TESTS.parents[2] / "spec"
EXAMPLES = SPEC / "examples"
OPENAPI = TESTS / "cli_fixtures" / "openapi.yaml"
URL = "https://cp.test"
TOKEN = "rbt_test_0123456789abcdef"  # a test token; the tests check it never reaches stdout
STEPS = ["detected", "license", "seal_check", "training", "predicting", "benchmarking", "gating", "evidence"]


def openapi() -> dict[str, Any]:
    from ruamel.yaml import YAML

    return YAML(typ="safe").load(OPENAPI.read_text(encoding="utf-8"))


def validator(component: str) -> jsonschema.Draft202012Validator:
    doc = openapi()
    reg = Registry().with_resource("urn:openapi", Resource.from_contents(doc, default_specification=DRAFT202012))
    return jsonschema.Draft202012Validator({"$ref": f"urn:openapi#/components/schemas/{component}"}, registry=reg)


def load(name: str, n: int, rel: str) -> Any:
    p = EXAMPLES / name / "rebases" / str(n) / rel
    return json.loads(p.read_text(encoding="utf-8")) if p.suffix == ".json" else read_yaml(p)


def _ts(base: str, minutes: int) -> str:
    t = datetime.fromisoformat(base.replace("Z", "+00:00")) + timedelta(minutes=minutes)
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Fixture:
    """One fixture rebase as the control plane would serve it."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.n = {"pass": 13, "blocked": 14}[name]
        self.meta = load(name, self.n, "rebase.yaml")
        self.scores = load(name, self.n, "scores.json")
        self.gate = load(name, self.n, "gate.json")
        self.receipt = load(name, self.n, "receipt.json")
        self.license = load(name, self.n, "license.json")
        self.contamination = load(name, self.n, "contamination.json")
        self.cost = load(name, self.n, "cost.json")
        self.approvals = load(name, self.n, "evidence/approvals.json")
        self.findings = [read_yaml(p) for p in sorted((EXAMPLES / name / "findings").glob("F-*.yaml"))]
        self.timeline = self._timeline()

    def _timeline(self) -> list[dict[str, Any]]:
        """State, time and a detail made from the fixture files: what the orchestrator (W17) reports."""
        p = self.receipt["predicate"]
        details = {
            "detected": f"new base {self.meta['base']['repo']} @{self.meta['base']['revision'][:12]}",
            "license": f"{self.license['license']} {self.license['decision']} by policy",
            "seal_check": f"seal seq {p['holdout']['seal_seq']} < training seq {p['audit_seq']}",
            "training": f"executor {p['executor']['name']}",
            "predicting": f"{p['holdout']['rows']} holdout rows",
            "benchmarking": f"{self.cost['total_eur']} EUR",
            "gating": f"gate {self.gate['status']}",
            "evidence": "pack v1",
        }
        out = [{"state": s, "at": _ts(self.meta["created_at"], i), "detail": details[s]} for i, s in enumerate(STEPS)]
        k = len(out)
        if self.gate["status"] == "pass":
            out.append({"state": "pr_opened", "at": _ts(self.meta["created_at"], k), "detail": f"PR #{self.meta['pr']['number']}"})
            out.append({"state": "awaiting_signoff", "at": _ts(self.meta["created_at"], k + 1)})
        else:
            ids = ", ".join(f["id"] for f in self.gate["findings"])
            out.append({"state": "blocked", "at": _ts(self.meta["created_at"], k), "detail": f"{ids} logged; alias unchanged"})
        return out

    def view(self, k: int) -> dict[str, Any]:
        """RebaseView after the first k timeline events. Scores and gate appear once gating has run."""
        tl = self.timeline[:k]
        state = tl[-1]["state"]
        done = STEPS.index("gating") < k if state in STEPS else True
        meta = {k2: v for k2, v in self.meta.items() if k2 != "pr"}
        meta["state"] = state
        pr = None
        if self.meta.get("pr") and state in ("pr_opened", "awaiting_signoff"):
            pr = {"number": self.meta["pr"]["number"], "url": self.meta["pr"]["url"], "check": "success",
                  "labels": ["awaiting-mrm-signoff"]}
        return {
            "rebase": self.n, "model": self.meta["model"], "state": state, "meta": meta,
            "scores": self.scores if done else None, "gate": self.gate if done else None,
            "contamination": self.contamination if done else None, "cost": self.cost if done else None,
            "license": self.license, "approvals": self.approvals if state == "awaiting_signoff" else None,
            "receipt": self.receipt if done else None,
            "findings": [f for f in self.findings if f.get("rebase") == self.n] if done else [],
            "pr": pr, "timeline": tl, "synthetic": True,
        }

    def views(self) -> list[dict[str, Any]]:
        return [self.view(k) for k in range(1, len(self.timeline) + 1)]

    def model_detail(self) -> dict[str, Any]:
        cfg = read_yaml(EXAMPLES / self.name / "roadbed.yaml")
        rs = {"rebase": self.n, "model": cfg["model"], "state": self.timeline[-1]["state"],
              "created_at": self.meta["created_at"], "gate": self.gate["status"], "delta": self.scores["overall"]["delta"]}
        return {
            "summary": {"model": cfg["model"], "tenant": cfg["tenant"], "synthetic": True, "alias": cfg["alias"]["name"],
                        "version": cfg["alias"]["version"], "base": cfg["base"], "verdicts_this_week": self.receipt["predicate"]["snapshot"]["rows"],
                        "last_rebase": rs},
            "config": cfg, "versions": [], "recent_rebases": [rs],
        }


def runner_variant(view: dict[str, Any]) -> dict[str, Any]:
    """The pass view with the receipt's executor set to runner: only to render the 'trained on' line.
    The data stays synthetic: true, so the CLI still prints the SYNTHETIC label."""
    v = copy.deepcopy(view)
    if v.get("receipt"):
        v["receipt"]["predicate"]["executor"] = {"name": "runner", "version": "0.1.0"}
    return v


def alias_config(version: str, generation: int = 7) -> dict[str, Any]:
    """AliasConfig as W20 returns it. SYNTHETIC test values: upstream and adapter names are placeholders."""
    return {"spec": "roadbed/v0.1", "generation": generation, "aliases": {
        "claims-triage-prod": {"upstream": "http://vllm:8000/v1", "adapter": f"claims-triage-{version}", "version": version}}}


def problem(status: int, code: str, detail: str, **extra: Any) -> httpx.Response:
    body = {"type": f"https://roadbed.dev/problems/{code}", "title": code.replace("_", " "), "status": status,
            "code": code, "detail": detail, **extra}
    return httpx.Response(status, json=body, headers={"content-type": "application/problem+json"})


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated config (url https://cp.test, a token file) and an empty watch cache."""
    tok = tmp_path / "token"
    tok.write_text(TOKEN + "\n")
    tok.chmod(0o600)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'url = "{URL}"\ntoken_file = "{tok}"\ntrust_keys = []\n')
    monkeypatch.setenv("ROADBED_CONFIG", str(cfg))
    monkeypatch.setenv("ROADBED_CACHE_DIR", str(tmp_path / "cache"))
    for v in ("ROADBED_URL", "ROADBED_TOKEN_FILE"):
        monkeypatch.delenv(v, raising=False)
    return cfg


# ---------------------------------------------------------------- the fake follows the contract


def test_openapi_copy_matches_spec():
    """cli_fixtures/openapi.yaml was generated from these very spec schemas (platform contracts/sync.py hash)."""
    spec = {f.name.removesuffix(".schema.json"): json.loads(f.read_text()) for f in sorted((SPEC / "schemas/v0.1").glob("*.schema.json"))}
    assert openapi()["info"]["x-roadbed-spec"]["schemas_sha256"] == hashlib.sha256(rfc8785.dumps(spec)).hexdigest()


@pytest.mark.parametrize("name", ["pass", "blocked"])
def test_fake_bodies_match_openapi(name: str):
    f = Fixture(name)
    rv = validator("RebaseView")
    for v in f.views():
        rv.validate(v)
    rv.validate(runner_variant(f.views()[-1]))
    validator("ModelDetail").validate(f.model_detail())
    validator("AliasConfig").validate(alias_config("v13"))
    validator("ForbiddenProblem").validate(problem(403, "sod_violation", "you opened this candidate", rule="SoD-02", audit_seq=1412).json())


def test_openapi_validator_rejects_a_bad_view():
    bad = Fixture("pass").views()[-1]
    bad["state"] = "shipped"
    with pytest.raises(jsonschema.ValidationError):
        validator("RebaseView").validate(bad)


def test_fake_serves_fixture_numbers_unchanged():
    f = Fixture("pass")
    last = f.views()[-1]
    assert last["scores"] == json.loads((EXAMPLES / "pass/rebases/13/scores.json").read_text())
    assert last["receipt"]["predicate"]["executor"]["name"] == "mock" and last["synthetic"] is True
