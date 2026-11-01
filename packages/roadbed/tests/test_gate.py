"""roadbed.gate: the decision and its reasons, from real scores (no mocked engine anywhere)."""

from __future__ import annotations

import re

import pytest

from conftest import SCORERS, holdout_row, make_policy, pred, wrong_route
from roadbed.contracts import (
    ContaminationReport,
    GateContaminationInput,
    LicenseDecisionRecord,
    LicensePolicy,
    json_schema,
)
from roadbed.gate import decide_license, evaluate
from roadbed.hashing import sha256_canonical
from roadbed.stats import compare

LINES = ["auto_physical", "bodily_injury", "property", "liability", "workers_comp"]
STATES = ["CA", "TX", "FL", "NY", "PA", "IL"]
CHANNELS = ["broker", "direct", "partner"]
LICENSE = LicensePolicy(allow=["apache-2.0"])
H = "a" * 64


def _dataset(bi_drop: int):
    """600 rows over 14 slices; PA and IL get 12 rows each (low-n). bodily_injury has 120 rows."""
    hold = []
    for i in range(600):
        state = "PA" if i < 12 else "IL" if i < 24 else STATES[i % 4]
        hold.append(holdout_row(i, line=LINES[i % 5], state=state, channel=CHANNELS[i % 3]))
    inc = [pred(r) for r in hold]
    bi = [r for r in hold if r["slices"]["line"] == "bodily_injury"]
    drop = {r["id"] for r in bi[:bi_drop]}
    cand = [pred(r, wrong_route(r["expected"])) if r["id"] in drop else pred(r) for r in hold]
    return hold, inc, cand


def _scores(bi_drop: int, rebase: int = 13, policy=None):
    hold, inc, cand = _dataset(bi_drop)
    return compare(
        inc, cand, hold, ["line", "state", "channel"], policy or make_policy(),
        scorers=SCORERS, synthetic=True, model="claims-triage", rebase=rebase,
    )


def _report(rebase=13, shared=(), pairs=(), max_j=0.1, seal_ok=True, status=None) -> ContaminationReport:
    legs = {"audit_seq": seal_ok, "timestamp": True, "git_ancestor": True}
    clean = not shared and not pairs and seal_ok
    return ContaminationReport.model_validate({
        "spec": "roadbed/v0.1", "rebase": rebase, "status": status or ("pass" if clean else "fail"),
        "holdout": {"id": "H-1", "merkle_root": H, "rows": 600}, "train": {"id": "S-2026-11-01", "merkle_root": H, "rows": 80},
        "exact": {"shared": len(shared), "row_hashes": list(shared)},
        "near": {"method": "minhash", "num_perm": 128, "threshold": 0.8, "max_jaccard": max_j,
                 "pairs": [{"holdout": h, "train": t, "jaccard": j} for h, t, j in pairs]},
        "seal_order": {"seal_commit": "1" * 40, "train_commit": "2" * 40, **legs, "ok": seal_ok},
        "synthetic": True,
    })


def _license(decision: str, rebase=13, lic="apache-2.0") -> LicenseDecisionRecord:
    return LicenseDecisionRecord.model_validate({
        "spec": "roadbed/v0.1", "rebase": rebase, "base": {"repo": "Qwen/Qwen3.8-4B", "revision": "1" * 40},
        "license": lic, "source": "cardData", "decision": decision, "policy": "policy/license.yaml",
        "policy_sha256": H, "decided_at": "2026-11-01T10:00:00Z", "synthetic": True,
    })


def _check(g, name):
    return next(c for c in g.checks if c.name == name)


def _valid(g) -> None:
    import jsonschema

    jsonschema.Draft202012Validator(json_schema("gate")).validate(g.model_dump(mode="json", exclude_none=True))


# ---------------------------------------------------------------- pass and the plan's block cases


def test_pass_path():
    s = _scores(0)
    assert len(s.slices) == 14 and sum(x.low_n for x in s.slices) == 2
    g = evaluate(s, _report(), _license("allow"), make_policy())
    _valid(g)
    assert g.status == "pass" and g.findings == [] and g.synthetic and g.rebase == 13
    assert _check(g, "slice_margin").status == "pass"
    assert _check(g, "slice_margin").detail == "0 of 14 slices below -2.0 margin; 2 flagged low-n"
    assert _check(g, "license").detail == "apache-2.0 allowed by policy/license.yaml"
    assert _check(g, "contamination").detail == "0 shared row ids between train manifest and holdout"
    assert [c.name for c in g.checks] == ["slice_margin", "license", "contamination", "stage:shadow", "stage:canary", "stage:bias_tests"]
    assert g.policy_sha256 == sha256_canonical(make_policy().model_dump(mode="json", exclude_none=True))


def test_minus_nine_at_n120_blocks_and_logs_finding():
    s = _scores(18, rebase=14)  # 18 of 120 bodily_injury rows lose 60 -> -9.0
    bi = next(x for x in s.slices if x.value == "bodily_injury")
    assert bi.n == 120 and bi.delta == pytest.approx(-9.0)
    assert s.overall.delta > -2.0  # the overall number looks fine
    g = evaluate(s, _report(rebase=14), _license("allow", rebase=14), make_policy(), finding_offset=2)
    _valid(g)
    assert g.status == "blocked"
    sm = _check(g, "slice_margin")
    assert sm.status == "fail" and sm.detail == "1 of 14 slices below -2.0 margin (line=bodily_injury -9.0); 2 flagged low-n"
    [f] = g.findings
    assert f.id == "F-0003" and re.fullmatch(r"F-\d{4}", f.id)
    assert (f.rebase, f.slice, f.n, f.margin) == (14, "line=bodily_injury", 120, -2.0)
    assert f.delta == pytest.approx(-9.0) and f.ci_low <= f.delta <= f.ci_high < 0
    assert evaluate(s, None, None, make_policy()).findings[0].id == "F-0001"


def test_n12_slice_only_flags_low_n_and_passes():
    hold = [holdout_row(i, state="IL") for i in range(12)] + [holdout_row(100 + i, state="CA") for i in range(40)]
    inc = [pred(r) for r in hold]
    cand = [pred(r, wrong_route(r["expected"])) if r["slices"]["state"] == "IL" else pred(r) for r in hold]
    s = compare(inc, cand, hold, ["state"], make_policy(), scorers=SCORERS, synthetic=True, model="m", rebase=1)
    il = next(x for x in s.slices if x.value == "IL")
    assert il.n == 12 and il.delta == pytest.approx(-60.0) and il.low_n
    g = evaluate(s, None, None, make_policy())
    assert g.status == "pass" and g.findings == []
    assert g.checks[0].detail == "0 of 2 slices below -2.0 margin; 1 flagged low-n"


def test_three_leaked_rows_block():
    leaked = ("1" * 64, "2" * 64, "3" * 64)
    g = evaluate(_scores(0), _report(shared=leaked), _license("allow"), make_policy())
    _valid(g)
    assert g.status == "blocked" and g.findings == []
    c = _check(g, "contamination")
    assert c.status == "fail" and c.detail == "3 shared row ids between train manifest and holdout"


def test_near_pairs_and_seal_order_block_with_reasons():
    g = evaluate(_scores(0), _report(pairs=[(H, "b" * 64, 0.85)], max_j=0.85), None, make_policy())
    c = _check(g, "contamination")
    assert g.status == "blocked" and c.status == "fail"
    assert c.detail == "0 shared row ids between train manifest and holdout; 1 near-duplicate pairs (minhash Jaccard >= 0.8, max 0.85)"
    g = evaluate(_scores(0), _report(seal_ok=False), None, make_policy())
    c = _check(g, "contamination")
    assert g.status == "blocked" and c.detail.endswith("; seal does not precede training (audit_seq)")


def test_inconsistent_contamination_report_fails_closed():
    """Carry-over from W01: max_jaccard against the threshold is a runtime check JSON Schema cannot express."""
    forged = _report(max_j=0.93)  # status pass, no pairs, but the max similarity is above the threshold
    g = evaluate(_scores(0), forged, None, make_policy())
    c = _check(g, "contamination")
    assert g.status == "blocked" and c.status == "fail"
    assert "max_jaccard 0.93 >= threshold 0.8 but no pair is listed" in c.detail


def test_legacy_contamination_input_still_works():
    s = _scores(0)
    g = evaluate(s, GateContaminationInput(train_manifest="m", shared_ids=["a", "b", "c"]), None, make_policy())
    assert g.status == "blocked" and _check(g, "contamination").detail == "3 shared row ids between train manifest and holdout"
    assert evaluate(s, GateContaminationInput(), None, make_policy()).status == "pass"
    assert _check(evaluate(s, None, None, make_policy()), "contamination").status == "skipped"


# ---------------------------------------------------------------- license as a gate input


@pytest.mark.parametrize(
    "decision,status,detail",
    [
        ("allow", "pass", "apache-2.0 allowed by policy/license.yaml"),
        ("deny", "blocked", "apache-2.0 denied by policy/license.yaml"),
        ("review", "blocked", "apache-2.0 needs review under policy/license.yaml"),
    ],
)
def test_license_record_decision(decision, status, detail):
    g = evaluate(_scores(0), None, _license(decision), make_policy())
    assert g.status == status and _check(g, "license").detail == detail


def test_license_input_from_policy():
    lic = decide_license("llama3", LICENSE)
    assert not lic.allowed
    g = evaluate(_scores(0), None, lic, make_policy())
    c = _check(g, "license")
    assert g.status == "blocked" and c.status == "fail" and c.detail == "llama3 denied by policy/license.yaml"
    assert decide_license("Apache-2.0", LICENSE).allowed


def test_failed_check_not_in_block_on_warns():
    pol = make_policy(block_on=["contamination"])
    g = evaluate(_scores(18, policy=pol), None, decide_license("llama3", LICENSE), pol)
    assert g.status == "pass"
    assert {c.name: c.status for c in g.checks}["slice_margin"] == "warn"
    assert {c.name: c.status for c in g.checks}["license"] == "warn"
    assert len(g.findings) == 1  # finding is still logged


# ---------------------------------------------------------------- inputs that disagree


@pytest.mark.parametrize("change", [{"seed": 1}, {"n_boot": 500}, {"ci": 0.9}, {"margin": -10.0}, {"min_n": 5}])
def test_policy_must_match_the_scores(change):
    with pytest.raises(ValueError):
        evaluate(_scores(18), None, None, make_policy(**change))


def test_records_for_another_rebase_raise():
    with pytest.raises(ValueError, match="rebase 99"):
        evaluate(_scores(0), _report(rebase=99), None, make_policy())
    with pytest.raises(ValueError, match="rebase 99"):
        evaluate(_scores(0), None, _license("allow", rebase=99), make_policy())
