"""Remote commands against a respx fake of the control plane (test_cli_fakeapi.py): rebase, status, seal, promote,
rollback, init --check. The fake serves spec fixture files; the tests check what the CLI prints, sends and exits with.
Server-side rules (SoD, RBAC, promote preconditions) belong to the server; here a fake deny only checks that the CLI reports
the server's code, rule and audit seq and exits 3. Nothing here claims the server enforces them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from roadbed.cli.main import app
from test_cli_fakeapi import EXAMPLES, TOKEN, URL, Fixture, alias_config, cli_env, problem, runner_variant  # noqa: F401

SNAP = Path(__file__).resolve().parent / "cli_fixtures" / "snapshots"
REV = "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"
ARGS = ["rebase", "claims-triage", "--on", "Qwen/Qwen3.8-4B", "--revision", REV, "--interval", "0"]
cli = CliRunner()

# roadbed-website public/index.html (commit 1b13725), the "terminal illustrative" block after `roadbed rebase`.
# The site's numbers are illustrative; these tests compare the shape of each line, the numbers come from the fixtures.
WEBSITE = [
    "trained on 38,410 verdicts",
    "scored on 1,284 holdout cases",
    "+2.1 (CI +0.9 to +3.3)",
    "0 of 14 slices below margin",
    "opened rebase #13 → awaiting MRM sign-off",
]


def shape(line: str) -> str:
    """A line with every number (sign, digits, commas, decimal point) replaced by N."""
    return re.sub(r"[+−-]?\d[\d,]*(\.\d+)?", "N", line)


def serve(m: respx.MockRouter, views: list[dict], status: int = 202) -> respx.Route:
    m.post("/v1/models/claims-triage/rebases").mock(return_value=httpx.Response(status, json=views[0]))
    n = views[0]["rebase"]
    return m.get(f"/v1/rebases/{n}").mock(side_effect=[httpx.Response(200, json=v) for v in views[1:]])


def run_rebase(views: list[dict], *extra: str, status: int = 202) -> tuple:
    """(result, the requests the CLI sent)."""
    with respx.mock(base_url=URL, assert_all_called=False) as m:
        serve(m, views, status)
        r = cli.invoke(app, [*ARGS, *extra])
        return r, [c.request for c in m.calls]


# ---------------------------------------------------------------- rebase


@pytest.mark.parametrize("name,code", [("pass", 0), ("blocked", 4)])
def test_rebase_stream_snapshot(cli_env, name: str, code: int):
    r, sent = run_rebase(Fixture(name).views())
    assert r.exit_code == code, r.output
    assert r.output == (SNAP / f"rebase_remote_{name}.txt").read_text(encoding="utf-8")
    # one POST, then one GET per new state until the terminal one
    assert len(sent) == len(Fixture(name).timeline)


def test_rebase_request_body_and_auth(cli_env):
    r, sent = run_rebase(Fixture("pass").views())
    post = sent[0]
    assert json.loads(post.content) == {"base": {"repo": "Qwen/Qwen3.8-4B", "revision": REV}, "trigger": "manual"}
    assert post.headers["authorization"] == f"Bearer {TOKEN}"
    assert all(q.headers["authorization"] == f"Bearer {TOKEN}" for q in sent)
    assert TOKEN not in r.output


def test_rebase_summary_numbers_come_from_scores_json(cli_env):
    """Known answer: each printed number is the fixture scores.json value, rounded to one decimal."""
    s = json.loads((EXAMPLES / "pass/rebases/13/scores.json").read_text())
    o = s["overall"]
    r, _ = run_rebase(Fixture("pass").views())

    def sg(x: float) -> str:
        v = round(x, 1)
        return "0.0" if v == 0 else ("+" if v > 0 else "−") + f"{abs(v):.1f}"

    assert f"scored on {o['n']:,} holdout cases" in r.output
    assert f"{sg(o['delta'])} (CI {sg(o['ci_low'])} to {sg(o['ci_high'])})" in r.output
    below = sum(x["below_margin"] for x in s["slices"])
    low = sum(x["low_n"] for x in s["slices"])
    assert f"{below} of {len(s['slices'])} slices below margin" in r.output
    assert f"{low} slices flagged low-n" in r.output


def test_rebase_mock_receipt_is_labelled_and_never_says_trained(cli_env):
    r, _ = run_rebase(Fixture("pass").views())
    assert "MOCK executor" in r.output and "SYNTHETIC data" in r.output
    assert "trained on" not in r.output


def test_rebase_output_matches_website_transcript_shape(cli_env):
    """With a runner receipt the CLI prints the website's lines in the website's order (numbers from the fixture).
    The fake data stays synthetic, so the SYNTHETIC label is still printed."""
    views = [runner_variant(v) for v in Fixture("pass").views()]
    r, _ = run_rebase(views)
    assert r.exit_code == 0, r.output
    lines = [ln for ln in r.output.splitlines() if ln.strip()]
    got = [shape(ln) for ln in lines]
    idx = [got.index(shape(w)) for w in WEBSITE]
    assert idx == sorted(idx), (WEBSITE, lines)
    assert lines[-1] == "opened rebase #13 → awaiting MRM sign-off"
    assert "SYNTHETIC data" in lines and "trained on 80 verdicts" in lines  # receipt predicate.snapshot.rows


def test_rebase_blocked_final_line_names_slice_and_delta(cli_env):
    r, _ = run_rebase(Fixture("blocked").views())
    gate = json.loads((EXAMPLES / "blocked/rebases/14/gate.json").read_text())
    f = gate["findings"][0]
    last = r.output.strip().splitlines()[-1]
    assert last.startswith(f"blocked: {f['slice']} −{abs(f['delta']):.1f}") and f["id"] in last


def test_rebase_json(cli_env):
    r, _ = run_rebase(Fixture("blocked").views(), "--json")
    assert r.exit_code == 4
    d = json.loads(r.output)
    assert d["status"] == "blocked" and d["state"] == "blocked" and d["exit_code"] == 4
    assert d["labels"] == {"synthetic": True, "executor": "mock", "mock": True, "replay": False}
    assert d["final"].startswith("blocked: line=bodily_injury")
    assert [e["state"] for e in d["events"]] == [e["state"] for e in Fixture("blocked").timeline]
    assert d["overall"]["delta"] == json.loads((EXAMPLES / "blocked/rebases/14/scores.json").read_text())["overall"]["delta"]


def test_global_json_flag(cli_env):
    with respx.mock(base_url=URL) as m:
        serve(m, Fixture("pass").views())
        r = cli.invoke(app, ["--json", *ARGS])
    assert r.exit_code == 0 and json.loads(r.output)["status"] == "pass"


def test_rebase_existing_is_reported_and_followed(cli_env):
    r, _ = run_rebase(Fixture("pass").views(), status=200)
    assert r.output.startswith("existing rebase #13") and r.exit_code == 0


def test_rebase_no_follow(cli_env):
    r, sent = run_rebase(Fixture("pass").views(), "--no-follow")
    assert r.exit_code == 0 and len(sent) == 1
    assert r.output.strip().splitlines()[-1] == "rebase #13: detected"


def test_rebase_timeout_exits_3(cli_env):
    views = Fixture("pass").views()[:3]
    with respx.mock(base_url=URL, assert_all_called=False) as m:
        m.post("/v1/models/claims-triage/rebases").mock(return_value=httpx.Response(202, json=views[0]))
        m.get("/v1/rebases/13").mock(return_value=httpx.Response(200, json=views[2]))
        r = cli.invoke(app, [*ARGS, "--timeout", "0"])
    assert r.exit_code == 3 and "still detected after 0 s" in r.output, r.output


def test_rebase_failed_state_exits_3(cli_env):
    views = Fixture("pass").views()[:4]
    views[-1] = {**views[-1], "state": "failed",
                 "timeline": [*views[-1]["timeline"][:-1], {"state": "failed", "at": "2026-11-01T10:17:00Z", "detail": "cost cap reached"}]}
    r, _ = run_rebase(views)
    assert r.exit_code == 3 and r.output.strip().splitlines()[-1] == "failed: rebase #13 cost cap reached"


def test_rebase_server_sod_deny_is_reported_exit_3(cli_env):
    with respx.mock(base_url=URL) as m:
        m.post("/v1/models/claims-triage/rebases").mock(
            return_value=problem(403, "sod_violation", "you sealed the holdout", rule="SoD-01", audit_seq=1390))
        r = cli.invoke(app, ARGS)
        rj = cli.invoke(app, [*ARGS, "--json"])
    assert r.exit_code == 3 and "403 sod_violation SoD-01 (audit seq 1390): you sealed the holdout" in r.output
    d = json.loads(rj.output)
    assert rj.exit_code == 3 and d["remote"]["rule"] == "SoD-01" and d["remote"]["audit_seq"] == 1390


def test_rebase_unreachable_exits_3(cli_env):
    with respx.mock(base_url=URL) as m:
        m.post("/v1/models/claims-triage/rebases").mock(side_effect=httpx.ConnectError("refused"))
        r = cli.invoke(app, ARGS)
    assert r.exit_code == 3 and "ConnectError" in r.output


def test_rebase_redirect_not_followed(cli_env):
    with respx.mock(base_url=URL) as m:
        m.post("/v1/models/claims-triage/rebases").mock(return_value=httpx.Response(307, headers={"location": "https://evil.test/"}))
        r = cli.invoke(app, ARGS)
        assert len(m.calls) == 1
    assert r.exit_code == 3 and "not followed" in r.output


def test_rebase_contract_mismatch_exits_3(cli_env):
    with respx.mock(base_url=URL) as m:
        m.post("/v1/models/claims-triage/rebases").mock(return_value=httpx.Response(202, json={"hello": "world"}))
        r = cli.invoke(app, ARGS)
    assert r.exit_code == 3 and "does not match the contract" in r.output


def test_rebase_base_from_watch_cache(cli_env, tmp_path: Path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "watch.json").write_text(json.dumps({"last": "Qwen/Qwen3.*", "entries": {"Qwen/Qwen3.*": {
        "glob": "Qwen/Qwen3.*", "repo": "Qwen/Qwen3.8-4B", "sha": REV, "license": "apache-2.0", "decision": "allow",
        "fetched_at": "2026-11-01T09:00:00Z"}}}))
    with respx.mock(base_url=URL) as m:
        serve(m, Fixture("pass").views())
        r = cli.invoke(app, ["rebase", "claims-triage", "--interval", "0"])
        body = json.loads(m.calls[0].request.content)
    assert r.exit_code == 0, r.output
    assert body == {"base": {"repo": "Qwen/Qwen3.8-4B", "revision": REV}, "trigger": "watch"}


@pytest.mark.parametrize("args,msg", [
    (["rebase", "claims-triage"], "no base"),
    (["rebase", "claims-triage", "--on", "Qwen/Qwen3.8-4B"], "--revision"),
    (["rebase", "claims-triage", "--on", "Qwen/Qwen3.8-4B", "--revision", "main"], "--revision"),
])
def test_rebase_usage_errors_exit_2(cli_env, args, msg):
    with respx.mock(base_url=URL) as m:
        r = cli.invoke(app, args)
        assert not m.calls
    assert r.exit_code == 2 and msg in r.output


def test_no_config_exits_2(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROADBED_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.delenv("ROADBED_URL", raising=False)
    r = cli.invoke(app, ARGS)
    assert r.exit_code == 2 and "roadbed init" in r.output


def test_typer_usage_error_exits_2(cli_env):
    assert cli.invoke(app, ["promote"]).exit_code == 2
    assert cli.invoke(app, ["rebase"]).exit_code == 2


# ---------------------------------------------------------------- status --remote


def test_status_remote(cli_env):
    f = Fixture("pass")
    with respx.mock(base_url=URL) as m:
        m.get("/v1/models/claims-triage").mock(return_value=httpx.Response(200, json=f.model_detail()))
        r = cli.invoke(app, ["status", "--remote", "--model", "claims-triage"])
        rj = cli.invoke(app, ["status", "--remote", "--model", "claims-triage", "--json"])
    assert r.exit_code == 0, r.output
    assert r.output == (SNAP / "status_remote_pass.txt").read_text(encoding="utf-8")
    d = json.loads(rj.output)
    assert d["synthetic"] is True and d["rebases"][0]["delta"] == f.scores["overall"]["delta"]


def test_status_remote_model_from_asset(cli_env, monkeypatch):
    monkeypatch.chdir(EXAMPLES / "pass")
    with respx.mock(base_url=URL) as m:
        route = m.get("/v1/models/claims-triage").mock(return_value=httpx.Response(200, json=Fixture("pass").model_detail()))
        r = cli.invoke(app, ["status", "--remote"])
    assert r.exit_code == 0 and route.called


def test_model_name_is_one_path_segment(cli_env):
    with respx.mock(base_url=URL) as m:
        route = m.get("/v1/models/..%2Fv1%2Faudit").mock(return_value=problem(404, "not_found", "no model"))
        r = cli.invoke(app, ["status", "--remote", "--model", "../v1/audit"])
    assert route.called and r.exit_code == 3


# ---------------------------------------------------------------- seal


def test_seal(cli_env):
    seal_doc = json.loads((EXAMPLES / "pass/data/holdout/H-2026-11-01/SEAL.json").read_text())
    with respx.mock(base_url=URL) as m:
        route = m.post("/v1/holdouts").mock(return_value=httpx.Response(201, json=seal_doc))
        r = cli.invoke(app, ["seal", "--model", "claims-triage", "--snapshot", "S-2026-11-01", "--rows", "60", "--seed", "7",
                             "--by", "line", "--by", "state"])
    assert r.exit_code == 0, r.output
    assert json.loads(route.calls[0].request.content) == {
        "model": "claims-triage", "snapshot": "S-2026-11-01", "rows": 60, "seed": 7,
        "stratification": {"keys": ["line", "state"], "method": "proportional"}}
    assert (f"sealed {seal_doc['id']} by {seal_doc['sealed_by']} at {seal_doc['sealed_at']} · audit seq {seal_doc['audit_seq']:,} · "
            f"{seal_doc['rows']} rows") in r.output
    assert "SYNTHETIC data" in r.output


def test_seal_server_deny_is_reported_exit_3(cli_env):
    with respx.mock(base_url=URL) as m:
        m.post("/v1/holdouts").mock(return_value=problem(403, "sod_violation", "only a validator seals", rule="SoD-05", audit_seq=1203))
        r = cli.invoke(app, ["seal", "--model", "claims-triage", "--snapshot", "S-1", "--rows", "1", "--seed", "0", "--by", "line"])
    assert r.exit_code == 3 and "SoD-05" in r.output and "audit seq 1203" in r.output


def test_seal_bad_method_exits_2(cli_env):
    r = cli.invoke(app, ["seal", "--model", "m", "--snapshot", "S-1", "--rows", "1", "--seed", "0", "--by", "line", "--method", "random"])
    assert r.exit_code == 2


# ---------------------------------------------------------------- promote, rollback


def test_promote(cli_env, monkeypatch):
    monkeypatch.chdir(EXAMPLES / "pass")
    with respx.mock(base_url=URL) as m:
        route = m.post("/v1/aliases/claims-triage-prod:promote").mock(
            return_value=httpx.Response(200, json=alias_config("v13", 8), headers={"etag": '"g8-0123456789abcdef"'}))
        r = cli.invoke(app, ["promote", "13"])
    assert r.exit_code == 0, r.output
    assert r.output.strip() == 'claims-triage-prod → v13 (generation 8, etag "g8-0123456789abcdef")'
    req = route.calls[0].request
    assert json.loads(req.content) == {"rebase": 13}
    assert re.fullmatch(r"[0-9a-f-]{36}", req.headers["idempotency-key"])


def test_promote_retry_key_is_sent(cli_env):
    with respx.mock(base_url=URL) as m:
        route = m.post("/v1/aliases/claims-triage-prod:promote").mock(return_value=httpx.Response(200, json=alias_config("v13")))
        r = cli.invoke(app, ["promote", "13", "--alias", "claims-triage-prod", "--idempotency-key", "k-1", "--json"])
    assert route.calls[0].request.headers["idempotency-key"] == "k-1"
    assert json.loads(r.output)["version"] == "v13"


@pytest.mark.parametrize("resp,needle", [
    (lambda: problem(403, "sod_violation", "you opened this candidate", rule="SoD-02", audit_seq=1412), "403 sod_violation SoD-02 (audit seq 1412)"),
    (lambda: problem(409, "rebase_blocked", "rebase 14 is blocked"), "409 rebase_blocked: rebase 14 is blocked"),
    (lambda: problem(401, "unauthorized", "no valid token"), "401 unauthorized"),
])
def test_promote_server_refusals_are_reported_exit_3(cli_env, resp, needle):
    with respx.mock(base_url=URL) as m:
        m.post("/v1/aliases/claims-triage-prod:promote").mock(return_value=resp())
        r = cli.invoke(app, ["promote", "14", "--alias", "claims-triage-prod"])
    assert r.exit_code == 3 and needle in r.output


def test_rollback(cli_env):
    with respx.mock(base_url=URL) as m:
        route = m.post("/v1/aliases/claims-triage-prod:rollback").mock(return_value=httpx.Response(200, json=alias_config("v12", 9)))
        r = cli.invoke(app, ["rollback", "--alias", "claims-triage-prod", "--reason", "incident_regression", "--to", "v12"])
    assert r.exit_code == 0 and r.output.strip() == "claims-triage-prod → v12 (generation 9)"
    assert json.loads(route.calls[0].request.content) == {"reason_code": "incident_regression", "to_version": "v12"}


def test_rollback_free_text_reason_exits_2(cli_env):
    with respx.mock(base_url=URL) as m:
        r = cli.invoke(app, ["rollback", "--alias", "a", "--reason", "it broke, sorry"])
        assert not m.calls
    assert r.exit_code == 2


def test_alias_response_without_the_alias_exits_3(cli_env):
    with respx.mock(base_url=URL) as m:
        m.post("/v1/aliases/other:promote").mock(return_value=httpx.Response(200, json=alias_config("v13")))
        r = cli.invoke(app, ["promote", "13", "--alias", "other"])
    assert r.exit_code == 3


# ---------------------------------------------------------------- init --check


def test_init_check(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROADBED_CONFIG", str(tmp_path / "c.toml"))
    monkeypatch.delenv("ROADBED_URL", raising=False)
    tok = tmp_path / "t"
    tok.write_text(TOKEN)
    tok.chmod(0o600)
    with respx.mock(base_url=URL) as m:
        m.get("/v1/models").mock(return_value=httpx.Response(200, json={"items": [Fixture("pass").model_detail()["summary"]], "next_cursor": None}))
        r = cli.invoke(app, ["init", "--url", URL, "--token-file", str(tok), "--check"])
    assert r.exit_code == 0 and "control plane OK: 1 models visible" in r.output
    with respx.mock(base_url=URL) as m:
        m.get("/v1/models").mock(return_value=problem(401, "unauthorized", "token revoked"))
        r = cli.invoke(app, ["init", "--url", URL, "--token-file", str(tok), "--check", "--force"])
    assert r.exit_code == 3 and "token revoked" in r.output and TOKEN not in r.output
