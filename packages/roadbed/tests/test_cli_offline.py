"""Offline commands on the spec fixtures: eval reproduce, audit verify, evidence verify, and verify with config keys.

No network: every test here passes with outbound sockets blocked. Tampered copies must exit 4 and name the check.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import rfc8785
from typer.testing import CliRunner

from roadbed.cli.main import app
from test_asset_support import copy, reseal
from test_cli_fakeapi import cli_env  # noqa: F401

SPEC = Path(__file__).resolve().parents[3] / "spec"
EX = SPEC / "examples"
VECTORS = SPEC / "conformance" / "vectors" / "audit-chain.jsonl"
cli = CliRunner()


def jline(r) -> dict:
    return json.loads(r.output)


# ---------------------------------------------------------------- eval reproduce


def _by_hand(asset: Path, n: int) -> dict[str, float]:
    """Independent known answer: weighted field exact match (SPEC.md, Scoring 1-2) without roadbed.stats."""
    from roadbed.io import read_jsonl, read_yaml

    cfg = read_yaml(asset / "roadbed.yaml")
    sc = read_yaml(asset / cfg["evals"]["scorers"])
    w = {f["field"]: f["weight"] for f in sc["fields"]}
    hold = {r["id"]: r for r in read_jsonl(asset / cfg["data"]["holdout"]["path"] / "holdout.jsonl")}

    def mean(file: str) -> float:
        rows = read_jsonl(asset / f"rebases/{n}/{file}")
        tot = 0.0
        for p in rows:
            exp = hold[p["id"]]["expected"]
            tot += 100 * sum(wt * (p["output"].get(k) == exp.get(k)) for k, wt in w.items()) / sum(w.values())
        return tot / len(rows)

    inc, cand = mean("predictions.incumbent.jsonl"), mean("predictions.candidate.jsonl")
    return {"incumbent": inc, "candidate": cand, "delta": cand - inc, "n": len(hold)}


@pytest.mark.parametrize("asset,n", [(EX / "pass", 13), (EX / "blocked", 14), (EX / "claims-triage", 1)])
def test_eval_reproduce_fixtures_match(asset: Path, n: int):
    r = cli.invoke(app, ["eval", "reproduce", f"rebases/{n}", "--asset", str(asset), "--json"])
    assert r.exit_code == 0, r.output
    d = jline(r)
    assert d["check"]["status"] == "ok"
    hand = _by_hand(asset, n)
    o = d["overall"]
    assert o["n"] == hand["n"]
    for k in ("incumbent", "candidate", "delta"):
        assert o[k] == pytest.approx(hand[k], abs=1e-9)
    stored = json.loads((asset / f"rebases/{n}/scores.json").read_text())["overall"]
    assert (o["ci_low"], o["ci_high"]) == pytest.approx((stored["ci_low"], stored["ci_high"]), abs=1e-6)


def test_eval_reproduce_text_labels_mock_and_synthetic():
    r = cli.invoke(app, ["eval", "reproduce", "rebases/13", "--asset", str(EX / "pass")])
    assert r.exit_code == 0, r.output
    assert r.output.startswith("MOCK executor: these predictions did not come from a GPU run\nSYNTHETIC data\n")
    assert r.output.rstrip().endswith("scores recomputed: match  (overall + 9 slices, 6 decimals)")


def test_eval_reproduce_cwd_inside_asset(monkeypatch):
    monkeypatch.chdir(EX / "blocked")
    assert cli.invoke(app, ["eval", "reproduce", "rebases/14"]).exit_code == 0


def _flip_candidate(asset: Path, n: int, field: str) -> None:
    p = asset / f"rebases/{n}/predictions.candidate.jsonl"
    rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    rows[0]["output"][field] = "siu" if field == "route" and rows[0]["output"][field] != "siu" else 99
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_eval_reproduce_tampered_prediction_exits_4(tmp_path: Path):
    a = copy("pass", tmp_path / "a")
    _flip_candidate(a, 13, "route")
    r = cli.invoke(app, ["eval", "reproduce", "rebases/13", "--asset", str(a)])
    assert r.exit_code == 4
    assert "FAIL V-SCORES-MISMATCH  scores.json differs at overall." in r.output


def test_eval_reproduce_tampered_score_exits_4(tmp_path: Path):
    a = copy("blocked", tmp_path / "a")
    p = a / "rebases/14/scores.json"
    s = json.loads(p.read_text())
    s["slices"][1]["delta"] += 0.5
    p.write_text(json.dumps(s, indent=2))
    r = cli.invoke(app, ["eval", "reproduce", "rebases/14", "--asset", str(a), "--json"])
    d = jline(r)
    assert r.exit_code == 4 and d["check"]["code"] == "V-SCORES-MISMATCH" and "slices[1].delta" in d["check"]["detail"]


def test_eval_reproduce_digest_only_change_exits_4(tmp_path: Path):
    """A prediction file with the same scores but other bytes no longer matches the recorded sha256."""
    a = copy("pass", tmp_path / "a")
    p = a / "rebases/13/predictions.incumbent.jsonl"
    p.write_text(p.read_text() + "\n")
    r = cli.invoke(app, ["eval", "reproduce", "rebases/13", "--asset", str(a), "--json"])
    assert r.exit_code == 4 and "predictions.incumbent.sha256" in jline(r)["check"]["detail"]


@pytest.mark.parametrize(("field", "value"), [("synthetic", False), ("model", "someone-elses-model")])
def test_eval_reproduce_tampered_label_exits_4(tmp_path: Path, field: str, value: object):
    """synthetic and model are checked against roadbed.yaml, not copied from the file under test."""
    a = copy("pass", tmp_path / "a")
    p = a / "rebases/13/scores.json"
    s = json.loads(p.read_text())
    s[field] = value
    p.write_text(json.dumps(s, indent=2))
    r = cli.invoke(app, ["eval", "reproduce", "rebases/13", "--asset", str(a)])
    assert r.exit_code == 4, r.output
    assert f"scores.json differs at {field}" in r.output
    assert "SYNTHETIC data" in r.output


def test_eval_reproduce_without_receipt_says_executor_unknown():
    r = cli.invoke(app, ["eval", "reproduce", "rebases/1", "--asset", str(EX / "claims-triage")])
    assert r.exit_code == 0, r.output
    assert r.output.startswith("no receipt: executor unknown\nSYNTHETIC data\n")


def test_eval_reproduce_missing_dir_exits_2():
    r = cli.invoke(app, ["eval", "reproduce", "rebases/99", "--asset", str(EX / "pass")])
    assert r.exit_code == 2 and "not found" in r.output


# ---------------------------------------------------------------- audit verify


def _events() -> list[dict]:
    return [json.loads(x) for x in VECTORS.read_text().splitlines() if x.strip()]


def _write(path: Path, events: list[dict]) -> Path:
    path.write_text("".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events))
    return path


def _anchor(path: Path, head: dict, **kw) -> Path:
    doc = {"spec": "roadbed/v0.1", "date": head["ts"][:10], "head_seq": head["seq"], "head_hash": head["hash"], "count": 3,
           "prev_anchor": None, "anchored_at": "2026-11-02T00:00:05Z",
           "object_lock": {"bucket": "roadbed-audit", "key": f"anchors/{head['ts'][:10]}.json", "version_id": None},
           "synthetic": True, **kw}
    path.write_text(json.dumps(doc, indent=2))
    return path


def test_audit_vectors_known_answer():
    """Hash by hand: sha256 of RFC 8785 bytes of the event without 'hash' (not via roadbed code)."""
    for e in _events():
        body = {k: v for k, v in e.items() if k != "hash"}
        assert hashlib.sha256(rfc8785.dumps(body)).hexdigest() == e["hash"]
    r = cli.invoke(app, ["audit", "verify", "--export", str(VECTORS), "--json"])
    d = jline(r)
    assert r.exit_code == 0 and (d["events"], d["first_seq"], d["head_seq"], d["denied"]) == (3, 1, 3, 1)
    assert d["head_hash"] == _events()[-1]["hash"] and d["anchored"] == 0


def test_audit_verify_with_anchor(tmp_path: Path):
    a = _anchor(tmp_path / "2026-11-01.json", _events()[-1])
    r = cli.invoke(app, ["audit", "verify", "--export", str(VECTORS), "--anchor", str(a)])
    assert r.exit_code == 0, r.output
    assert r.output == (
        "chain OK  (3 events, seq 1..3, head e61697272c4e)\n"
        "head anchored: 2026-11-01 seq 3  (e61697272c4e)\n"
        "SYNTHETIC anchor\n"
        "audit verify OK  3 events, 1 denied, head anchored by 1 anchor(s)\n")


def test_audit_fixture_anchor_outside_export_is_skipped_not_passed():
    anchor = EX / "pass/audit/anchors/2026-11-01.json"
    r = cli.invoke(app, ["audit", "verify", "--export", str(VECTORS), "--anchor", str(anchor)])
    assert r.exit_code == 0 and "anchor 2026-11-01: skipped" in r.output and "head not anchored" in r.output
    r = cli.invoke(app, ["audit", "verify", "--export", str(VECTORS), "--anchor", str(anchor), "--strict"])
    assert r.exit_code == 4 and "V-AUDIT-ANCHOR" in r.output


def test_audit_no_anchor_strict_fails():
    assert cli.invoke(app, ["audit", "verify", "--export", str(VECTORS), "--strict"]).exit_code == 4


def _tamper(tmp_path: Path, fn) -> Path:
    ev = _events()
    fn(ev)
    return _write(tmp_path / "audit.jsonl", ev)


def _rehash(e: dict) -> None:
    e["hash"] = hashlib.sha256(rfc8785.dumps({k: v for k, v in e.items() if k != "hash"})).hexdigest()


@pytest.mark.parametrize("name,fn,code,where", [
    ("payload edited", lambda ev: ev[0]["payload"].update(rows=61), "V-AUDIT-HASH", "seq 1"),
    ("deny turned into allow", lambda ev: ev[2].update(outcome="allowed", rule=None), "V-AUDIT-HASH", "seq 3"),
    ("edited and re-hashed", lambda ev: (ev[1].update(actor="vik"), _rehash(ev[1])), "V-AUDIT-LINK", "seq 3"),
    ("event deleted", lambda ev: ev.pop(1), "V-AUDIT-SEQ", "seq 3"),
    ("events swapped", lambda ev: ev.insert(1, ev.pop(2)), "V-AUDIT-SEQ", "seq 3"),
    ("first prev_hash not zero", lambda ev: (ev[0].update(prev_hash="1" * 64), _rehash(ev[0])), "V-AUDIT-LINK", "seq 1"),
    ("email in payload", lambda ev: (ev[0]["payload"].update(user="ana@acme.test"), _rehash(ev[0])), "V-AUDIT-SCHEMA", "seq 1"),
    ("free-text key", lambda ev: (ev[0]["payload"].update(note="x"), _rehash(ev[0])), "V-AUDIT-SCHEMA", "seq 1"),
    ("deny without rule", lambda ev: (ev[2].update(rule=None), _rehash(ev[2])), "V-AUDIT-SCHEMA", "seq 3"),
])
def test_audit_tamper_exits_4_and_names_check(tmp_path: Path, name, fn, code, where):
    f = _tamper(tmp_path, fn)
    r = cli.invoke(app, ["audit", "verify", "--export", str(f)])
    assert r.exit_code == 4, (name, r.output)
    assert f"FAIL {code}" in r.output and where in r.output, (name, r.output)
    assert r.output.rstrip().endswith(f"audit verify failed: {code}")


def test_audit_anchor_mismatch(tmp_path: Path):
    head = dict(_events()[-1], hash="f" * 64)
    a = _anchor(tmp_path / "a.json", head)
    r = cli.invoke(app, ["audit", "verify", "--export", str(VECTORS), "--anchor", str(a), "--json"])
    d = jline(r)
    assert r.exit_code == 4 and d["failed"] == "V-AUDIT-ANCHOR" and "seq 3" in d["detail"]


def test_audit_anchor_chain(tmp_path: Path):
    ev = _events()
    first = json.loads(_anchor(tmp_path / "a1.json", ev[1], date="2026-10-31").read_text())
    good = _anchor(tmp_path / "a2.json", ev[2], prev_anchor=hashlib.sha256(rfc8785.dumps(first)).hexdigest())
    r = cli.invoke(app, ["audit", "verify", "--export", str(VECTORS), "--anchor", str(tmp_path / "a1.json"), "--anchor", str(good)])
    assert r.exit_code == 0 and "anchored by 2 anchor(s)" in r.output, r.output
    bad = _anchor(tmp_path / "a3.json", ev[2], prev_anchor="0" * 64)
    r = cli.invoke(app, ["audit", "verify", "--export", str(VECTORS), "--anchor", str(tmp_path / "a1.json"), "--anchor", str(bad)])
    assert r.exit_code == 4 and "prev_anchor" in r.output


def test_audit_bad_anchor_schema(tmp_path: Path):
    a = tmp_path / "a.json"
    a.write_text(json.dumps({"spec": "roadbed/v0.1", "date": "2026-11-01"}))
    r = cli.invoke(app, ["audit", "verify", "--export", str(VECTORS), "--anchor", str(a)])
    assert r.exit_code == 4 and "V-SCHEMA-INVALID" in r.output


def test_audit_partial_export(tmp_path: Path):
    f = _write(tmp_path / "tail.jsonl", _events()[1:])
    r = cli.invoke(app, ["audit", "verify", "--export", str(f)])
    assert r.exit_code == 0 and "chain start: skipped" in r.output
    assert cli.invoke(app, ["audit", "verify", "--export", str(f), "--strict"]).exit_code == 4


@pytest.mark.parametrize("text", ["", "not json\n", "[1,2]\n"])
def test_audit_garbage_exits_4(tmp_path: Path, text: str):
    f = tmp_path / "x.jsonl"
    f.write_text(text)
    r = cli.invoke(app, ["audit", "verify", "--export", str(f)])
    assert r.exit_code == 4 and "V-AUDIT-SCHEMA" in r.output


def test_audit_missing_export_exits_4(tmp_path: Path):
    r = cli.invoke(app, ["audit", "verify", "--export", str(tmp_path / "nope.jsonl")])
    assert r.exit_code == 4 and "V-AUDIT-SCHEMA" in r.output


# ---------------------------------------------------------------- evidence verify


def _ev(a: Path, n: int, *extra: str):
    return cli.invoke(app, ["evidence", "verify", str(a / f"rebases/{n}/evidence"), *extra])


@pytest.mark.parametrize("name,n,status", [("pass", 13, "approved: MRM sign-off complete"), ("blocked", 14, "blocked by the gate: no sign-off")])
def test_evidence_verify_fixtures(name: str, n: int, status: str, cli_env):
    r = _ev(EX / name, n)
    assert r.exit_code == 0, r.output
    assert r.output.rstrip().endswith(f"evidence verified rebase #{n}  {status}")
    assert "SYNTHETIC data" in r.output


def test_evidence_verify_accepts_rebase_dir_and_cwd(monkeypatch, cli_env):
    monkeypatch.chdir(EX / "pass")
    assert cli.invoke(app, ["evidence", "verify", "rebases/13"]).exit_code == 0


def _keyid() -> str:
    from roadbed.asset import keyid
    from roadbed.asset.signing import load_public

    return keyid(load_public(EX / "pass/policy/keys/evidence.pub"))


def test_evidence_trusted_key_from_flag_and_config(tmp_path: Path, cli_env):
    kid = _keyid()
    assert _ev(EX / "pass", 13, "--trust-key", kid).exit_code == 0
    r = _ev(EX / "pass", 13, "--trust-key", "0" * 64)
    assert r.exit_code == 4 and "V-SIG-UNTRUSTED" in r.output
    cli_env.write_text(cli_env.read_text().replace("trust_keys = []", f'trust_keys = ["{"1" * 64}"]'))
    r = _ev(EX / "pass", 13)
    assert r.exit_code == 4 and "V-SIG-UNTRUSTED" in r.output
    cli_env.write_text(cli_env.read_text().replace("1" * 64, kid))
    r = _ev(EX / "pass", 13)
    assert r.exit_code == 0 and "pinned" in r.output and "not pinned" not in r.output


def _edit_approvals(a: Path, n: int, fn) -> None:
    p = a / f"rebases/{n}/evidence/approvals.json"
    d = json.loads(p.read_text())
    fn(d)
    p.write_text(json.dumps(d, indent=2) + "\n")


def test_evidence_tampered_file_hash(tmp_path: Path, cli_env):
    a = copy("pass", tmp_path / "a")
    _edit_approvals(a, 13, lambda d: d.update(status_text="approved"))
    r = _ev(a, 13)
    assert r.exit_code == 4 and "FAIL V-HASH-MISMATCH  rebases/13/evidence/approvals.json" in r.output


def test_evidence_rehashed_but_not_resigned(tmp_path: Path, cli_env):
    a = copy("pass", tmp_path / "a")
    _edit_approvals(a, 13, lambda d: d.update(status_text="approved"))
    reseal(a, 13, sign=False, relock=False)
    r = _ev(a, 13)
    assert r.exit_code == 4 and "FAIL V-SIG-INVALID" in r.output


def test_evidence_rebase_file_tampered(tmp_path: Path, cli_env):
    """The signature covers the rebase manifest, so a changed rebase file fails even with the evidence intact."""
    a = copy("pass", tmp_path / "a")
    p = a / "rebases/13/scores.json"
    p.write_text(p.read_text().replace('"seed": 20261101', '"seed": 20261102'))
    r = _ev(a, 13)
    assert r.exit_code == 4 and "FAIL V-HASH-MISMATCH  rebases/13/scores.json does not match rebases/13/MANIFEST.sha256" in r.output


def test_evidence_unlisted_file(tmp_path: Path, cli_env):
    a = copy("pass", tmp_path / "a")
    (a / "rebases/13/evidence/extra.pdf").write_bytes(b"%PDF-1.4 SYNTHETIC")
    r = _ev(a, 13)
    assert r.exit_code == 4 and "V-MANIFEST-INCOMPLETE" in r.output and "extra.pdf" in r.output


def test_evidence_missing_file(tmp_path: Path, cli_env):
    a = copy("blocked", tmp_path / "a")
    (a / "rebases/14/predictions.candidate.jsonl").unlink()
    r = _ev(a, 14)
    assert r.exit_code == 4 and "V-HASH-MISSING" in r.output


def test_evidence_resigned_bad_approvals_fail_schema(tmp_path: Path, cli_env):
    """An attacker with the keys re-signs approvals for another rebase: hashes and signature pass, approvals do not."""
    a = copy("pass", tmp_path / "a")
    _edit_approvals(a, 13, lambda d: d.update(rebase=99))
    reseal(a, 13, relock=False)
    r = _ev(a, 13)
    assert r.exit_code == 4 and "FAIL V-SCHEMA-INVALID" in r.output and "rebase 99" in r.output
    _edit_approvals(a, 13, lambda d: (d.update(rebase=13), d["approvals"]["vik"].update(audit_seq=1)))
    reseal(a, 13, relock=False)
    r = _ev(a, 13)
    assert r.exit_code == 4 and "FAIL V-SCHEMA-INVALID" in r.output


def test_evidence_no_manifest(tmp_path: Path, cli_env):
    a = copy("pass", tmp_path / "a")
    (a / "rebases/13/evidence/MANIFEST.sha256").unlink()
    r = _ev(a, 13, "--json")
    assert r.exit_code == 4 and jline(r)["failed"] == "V-MANIFEST-MISSING"


def test_evidence_not_an_evidence_dir_exits_2(tmp_path: Path, cli_env):
    assert cli.invoke(app, ["evidence", "verify", str(tmp_path)]).exit_code == 2
    a = copy("pass", tmp_path / "a")
    shutil.rmtree(a / "rebases/13/evidence")
    assert _ev(a, 13).exit_code == 2


# ---------------------------------------------------------------- verify reads trust_keys from config


def test_verify_uses_config_trust_keys(cli_env):
    cli_env.write_text(cli_env.read_text().replace("trust_keys = []", f'trust_keys = ["{"2" * 64}"]'))
    r = cli.invoke(app, ["verify", "rebases/13", "--asset", str(EX / "pass")])
    assert r.exit_code == 4 and "V-SIG-UNTRUSTED" in r.output
