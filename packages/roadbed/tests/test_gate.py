from __future__ import annotations

import re

import pytest

from roadbed.contracts import LicensePolicy
from roadbed.gate import check_contamination, decide_license, evaluate
from roadbed.hashing import sha256_canonical
from roadbed.stats import compare

from conftest import SCORERS, holdout_row, make_policy, pred, wrong_route

LINES = ["auto_physical", "bodily_injury", "property", "liability", "workers_comp"]
STATES = ["CA", "TX", "FL", "NY", "PA", "IL"]
CHANNELS = ["broker", "direct", "partner"]
LICENSE = LicensePolicy(allow=["apache-2.0"])


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


def _scores(bi_drop: int, rebase: int = 13):
    hold, inc, cand = _dataset(bi_drop)
    return compare(
        hold, inc, cand, ["line", "state", "channel"], make_policy(),
        scorers=SCORERS, synthetic=True, model="claims-triage", rebase=rebase,
    )


def test_pass_path():
    s = _scores(0)
    assert len(s.slices) == 14 and sum(x.low_n for x in s.slices) == 2
    g = evaluate(s, make_policy(), decide_license("Apache-2.0", LICENSE), None)
    assert g.status == "pass" and g.findings == [] and g.synthetic and g.rebase == 13
    checks = {c.name: c for c in g.checks}
    assert checks["slice_margin"].status == "pass"
    assert checks["slice_margin"].detail == "0 of 14 slices below -2.0 margin; 2 flagged low-n"
    assert checks["license"].detail == "apache-2.0 allowed by policy/license.yaml"
    assert checks["contamination"].status == "skipped"
    assert [c.name for c in g.checks][3:] == ["stage:shadow", "stage:canary", "stage:bias_tests"]
    assert g.policy_sha256 == sha256_canonical(make_policy().model_dump(mode="json", exclude_none=True))


def test_blocked_path_and_finding_ids():
    s = _scores(18, rebase=14)  # 18 of 120 bodily_injury rows lose 60 -> -9.0
    bi = next(x for x in s.slices if x.value == "bodily_injury")
    assert bi.n == 120 and bi.delta == pytest.approx(-9.0)
    assert s.overall.delta > -2.0  # the overall number looks fine
    g = evaluate(s, make_policy(), decide_license("apache-2.0", LICENSE), finding_offset=2)
    assert g.status == "blocked"
    sm = g.checks[0]
    assert sm.status == "fail" and sm.detail == "1 of 14 slices below -2.0 margin (line=bodily_injury -9.0); 2 flagged low-n"
    [f] = g.findings
    assert f.id == "F-0003" and re.fullmatch(r"F-\d{4}", f.id)
    assert (f.rebase, f.slice, f.n, f.margin) == (14, "line=bodily_injury", 120, -2.0)
    assert f.ci_low <= f.delta <= f.ci_high
    # deterministic
    assert evaluate(s, make_policy(), finding_offset=2).findings == g.findings
    assert evaluate(s, make_policy()).findings[0].id == "F-0001"


def test_license_denied_blocks():
    lic = decide_license("llama3", LICENSE)
    assert not lic.allowed
    g = evaluate(_scores(0), make_policy(), lic)
    assert g.status == "blocked"
    c = next(c for c in g.checks if c.name == "license")
    assert c.status == "fail" and c.detail == "llama3 denied by policy/license.yaml"


def test_contamination_three_shared_ids_blocks():
    s = _scores(0)
    hold, _, _ = _dataset(0)
    holdout_ids = [r["id"] for r in hold]
    cont = check_contamination(["x-1", *holdout_ids[:3], "x-2"], holdout_ids, train_manifest="train/manifest.jsonl")
    assert cont.shared_ids == sorted(holdout_ids[:3])
    g = evaluate(s, make_policy(), decide_license("apache-2.0", LICENSE), cont)
    assert g.status == "blocked"
    c = next(c for c in g.checks if c.name == "contamination")
    assert c.status == "fail" and c.detail == "3 shared row ids between train manifest and holdout"
    assert check_contamination(None, holdout_ids) is None
    clean = evaluate(s, make_policy(), None, check_contamination(["x-1"], holdout_ids))
    assert clean.status == "pass"


def test_failed_check_not_in_block_on_warns():
    pol = make_policy(block_on=["contamination"])
    g = evaluate(_scores(18), pol, decide_license("llama3", LICENSE))
    assert g.status == "pass"
    assert {c.name: c.status for c in g.checks}["slice_margin"] == "warn"
    assert {c.name: c.status for c in g.checks}["license"] == "warn"
    assert len(g.findings) == 1  # finding is still logged


def test_low_n_slice_never_blocks():
    hold = [holdout_row(i, state="IL") for i in range(12)] + [holdout_row(100 + i, state="CA") for i in range(40)]
    inc = [pred(r) for r in hold]
    cand = [pred(r, wrong_route(r["expected"])) if r["slices"]["state"] == "IL" else pred(r) for r in hold]
    s = compare(hold, inc, cand, ["state"], make_policy(), scorers=SCORERS, synthetic=True, model="m", rebase=1)
    g = evaluate(s, make_policy())
    assert g.status == "pass" and g.findings == []
    assert g.checks[0].detail == "0 of 2 slices below -2.0 margin; 1 flagged low-n"
