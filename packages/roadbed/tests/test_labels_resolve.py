"""Label resolver: label_rules@v1 on hand-made events and on the 2k demo fixture."""

from __future__ import annotations

import dataclasses
import hashlib
import random

import pytest
import rfc8785
from test_evals_support import AS_OF, events, input_ref, interaction, outcome, output_schema, verdict

from roadbed.asset import row_hash
from roadbed.labels import LABEL_RULES_V1, PINNED, LabelRules, ResolveError, RulesNotPinned, resolve, summary, weight


def one(rows: list[dict]) -> dict:
    assert len(rows) == 1
    return rows[0]


# ---------------------------------------------------------------- rules


def test_rules_v1_hash_is_pinned():
    assert LABEL_RULES_V1.sha256 == PINNED["label_rules@v1"]
    assert LABEL_RULES_V1.weights["fix"] == 2.0 and LABEL_RULES_V1.weights["approve"] == 1.0
    assert set(LABEL_RULES_V1.eval_only) == {"reject", "escalate"}


def test_changed_rules_under_the_v1_name_are_refused():
    bent = dataclasses.replace(LABEL_RULES_V1, weights={**LABEL_RULES_V1.weights, "fix": 3.0})
    with pytest.raises(RulesNotPinned, match="pinned"):
        resolve([interaction("a")], [verdict("a", "approve")], [], bent)
    unknown = dataclasses.replace(LABEL_RULES_V1, version="label_rules@v2")
    with pytest.raises(RulesNotPinned, match="not a pinned"):
        resolve([interaction("a")], [verdict("a", "approve")], [], unknown)


def test_rules_note_does_not_change_the_pin():
    assert dataclasses.replace(LABEL_RULES_V1, note="other words").sha256 == LABEL_RULES_V1.sha256


# ---------------------------------------------------------------- verdicts and outcomes


def test_approve_labels_with_the_output_and_fix_with_the_correction():
    rows = resolve(
        [interaction("a", output={"route": "standard", "severity": 2}), interaction("b", output={"route": "standard", "severity": 2})],
        [verdict("a", "approve"), verdict("b", "fix", fix={"route": "siu", "severity": 4}, reason="fraud_missed")], [],
    )
    a, b = rows
    assert a["verdict"] == {"route": "standard", "severity": 2} and a["reason_code"] is None and a["weight"] == 1.0
    assert b["verdict"] == {"route": "siu", "severity": 4} and b["fix"] == b["verdict"] and b["reason_code"] == "fraud_missed"
    assert b["weight"] == 2.0 and b["use"] == "train" and b["action"] == "fix"


def test_late_outcome_wins_over_the_verdict():
    rows = resolve(
        [interaction("a", output={"route": "standard", "severity": 2})],
        [verdict("a", "approve", ts="2026-10-02T10:00:00Z")],
        [outcome("a", {"final_route": "complex", "paid_usd": 120.5}, observed_at="2026-10-20T10:00:00Z")],
    )
    r = one(rows)
    assert r["verdict"] == {"route": "complex", "severity": 2}
    assert r["label_source"] == "outcome"
    assert r["outcome"] == {"final_route": "complex", "paid_usd": 120.5}


def test_late_outcome_also_overrides_a_fix():
    r = one(resolve(
        [interaction("a")], [verdict("a", "fix", fix={"route": "standard", "severity": 3}, ts="2026-10-02T10:00:00Z")],
        [outcome("a", {"final_route": "siu"}, observed_at="2026-10-03T10:00:00Z")],
    ))
    assert r["verdict"] == {"route": "siu", "severity": 3}
    assert r["fix"] == {"route": "standard", "severity": 3}  # the correction test keeps the reviewer's fix
    assert r["weight"] == 2.0


def test_outcome_before_the_verdict_does_not_override():
    r = one(resolve(
        [interaction("a")], [verdict("a", "fix", fix={"route": "standard", "severity": 3}, ts="2026-10-05T10:00:00Z")],
        [outcome("a", {"final_route": "siu"}, observed_at="2026-10-03T10:00:00Z")],
    ))
    assert r["verdict"] == {"route": "standard", "severity": 3} and r["label_source"] == "verdict"
    assert r["outcome"] == {"final_route": "siu"}


def test_latest_outcome_and_latest_verdict_are_used():
    r = one(resolve(
        [interaction("a")],
        [verdict("a", "fix", ts="2026-10-02T10:00:00Z", fix={"route": "complex", "severity": 3}),
         verdict("a", "approve", ts="2026-10-04T10:00:00Z")],
        [outcome("a", {"final_route": "siu"}, observed_at="2026-10-10T10:00:00Z"),
         outcome("a", {"final_route": "fast_track"}, observed_at="2026-10-12T10:00:00Z")],
    ))
    assert r["action"] == "approve" and r["verdict"] == {"route": "fast_track", "severity": 2}


def test_outcome_keys_that_name_no_label_field_change_nothing():
    r = one(resolve([interaction("a")], [verdict("a", "approve")], [outcome("a", {"paid_usd": 10.0, "siu_referral": False})]))
    assert r["verdict"] == {"route": "standard", "severity": 2} and r["label_source"] == "verdict"


def test_reject_and_escalate_are_for_evals_only():
    rows = resolve([interaction("a"), interaction("b")], [verdict("a", "reject", reason="bad_output"), verdict("b", "escalate")],
                   [outcome("a", {"final_route": "siu"})])
    assert [r["use"] for r in rows] == ["eval", "eval"]
    assert all(r["verdict"] is None and r["weight"] == 0.0 and r["label_source"] is None for r in rows)


def test_as_of_hides_later_events():
    ints = [interaction("a", ts="2026-10-01T10:00:00Z"), interaction("late", ts="2026-11-02T10:00:00Z")]
    vds = [verdict("a", "approve", ts="2026-10-02T10:00:00Z"), verdict("a", "fix", ts="2026-11-03T10:00:00Z"),
           verdict("late", "approve", ts="2026-11-03T10:00:00Z")]
    ocs = [outcome("a", {"final_route": "siu"}, observed_at="2026-11-05T10:00:00Z")]
    r = one(resolve(ints, vds, ocs, as_of="2026-11-01T09:00:00Z"))
    assert r["interaction_id"] == "a" and r["action"] == "approve" and r["outcome"] is None
    assert len(resolve(ints, vds, ocs)) == 2


def test_unreviewed_interactions_are_not_rows():
    assert resolve([interaction("a"), interaction("b")], [verdict("a", "approve")], []) == resolve(
        [interaction("a")], [verdict("a", "approve")], [])


def test_row_hash_is_the_spec_row_hash():
    r = one(resolve([interaction("a")], [verdict("a", "fix")], [outcome("a", {"final_route": "siu"})]))

    want = hashlib.sha256(rfc8785.dumps({k: r[k] for k in ("interaction_id", "input_ref", "output", "verdict", "reason_code", "outcome", "ts")})).hexdigest()
    assert r["row_hash"] == want == row_hash(r)


def test_weight_follows_from_a_snapshot_row():
    rows = resolve([interaction("a"), interaction("b")], [verdict("a", "approve"), verdict("b", "fix")], [])
    snap = [{k: r[k] for k in ("interaction_id", "reason_code")} for r in rows]
    assert [weight(s) for s in snap] == [1.0, 2.0] == [weight(r) for r in rows]


# ---------------------------------------------------------------- idempotency and contracts


def test_retries_are_no_ops_and_conflicts_are_errors():
    v = verdict("a", "approve", vid="v1")
    assert resolve([interaction("a")] * 2, [v, dict(v)], []) == resolve([interaction("a")], [v], [])
    with pytest.raises(ResolveError, match="different content"):
        resolve([interaction("a")], [v, {**v, "action": "reject"}], [])
    with pytest.raises(ResolveError, match="different content"):
        resolve([interaction("a"), interaction("a", output={"route": "siu", "severity": 5})], [v], [])


def test_bodies_must_meet_their_contract():
    bad_fix = verdict("a", "approve")
    bad_fix["action"] = "fix"  # a fix without verdict and reason_code
    with pytest.raises(ResolveError, match="verdict"):
        resolve([interaction("a")], [bad_fix], [])
    no_profile = interaction("a")
    del no_profile["profile_id"]
    with pytest.raises(ResolveError, match="profile_id"):
        resolve([no_profile], [verdict("a", "approve")], [])
    with pytest.raises(ResolveError, match="actor"):
        resolve([interaction("a")], [verdict("a", "fix", actor="vik@acme.example")], [])
    with pytest.raises(ResolveError, match="as_of"):
        resolve([interaction("a")], [verdict("a", "approve")], [], as_of="2026-11-01")


def test_actor_is_kept_for_the_correction_test():
    r = one(resolve([interaction("a")], [verdict("a", "fix", actor="vik")], []))
    assert r["actor"] == "vik"


def test_input_order_does_not_matter():
    ints, vds, ocs = events("interactions")[:300], events("verdicts")[:300], events("outcomes")[:200]
    want = resolve(ints, vds, ocs)
    rng = random.Random(1)
    for _ in range(3):
        rng.shuffle(ints), rng.shuffle(vds), rng.shuffle(ocs)
        assert resolve(ints, vds, ocs) == want


# ---------------------------------------------------------------- quarantine (no PII past the resolver)


@pytest.mark.parametrize("change, reason", [
    (lambda i, v, o: i["input"].__setitem__("narrative", "call jane.doe@example.com about the claim"), "email"),
    (lambda i, v, o: i["input"].__setitem__("narrative", "call back on +1 (415) 555-0134 tomorrow"), "phone"),
    (lambda i, v, o: i["input"].__setitem__("narrative", "ssn 123-45-6789 on file"), "ssn"),
    (lambda i, v, o: o["outcome"].__setitem__("adjuster_phone", "415-555-0134"), "phone"),
    (lambda i, v, o: o["outcome"].__setitem__("email", "x"), "key 'email'"),
    (lambda i, v, o: o["outcome"].__setitem__("adjuster", "Jane Doe"), "not a token"),
    (lambda i, v, o: v["verdict"].__setitem__("route", "the claimant told me on the phone that her neighbour saw it happen"), "free text"),
    (lambda i, v, o: i["output"].__setitem__("route", "jane.doe@example.com"), "email"),
    (lambda i, v, o: v["verdict"].__setitem__("severity", 9), "output schema"),
])
def test_rows_with_pii_or_free_text_are_quarantined(change, reason):
    i, v, o = interaction("a"), verdict("a", "fix", ts="2026-10-02T10:00:00Z"), outcome("a", {"final_route": "siu"})
    change(i, v, o)
    i["input_ref"] = input_ref(i["input"])
    r = one(resolve([i], [v], [o], output_schema=output_schema()))
    assert r["use"] == "quarantine" and reason in r["reason"]
    assert set(r) == {"interaction_id", "use", "reason"}  # nothing else travels with it
    assert summary([r])["quarantined"] == ["a"]


def test_input_ref_that_does_not_match_the_input_is_quarantined():
    i = interaction("a")
    i["input"]["narrative"] += " (edited after hashing)"
    r = one(resolve([i], [verdict("a", "approve")], []))
    assert r["use"] == "quarantine" and "input_ref" in r["reason"]


def test_erased_subjects_are_left_out():
    rows = resolve([interaction("a", subject_token="tok_00aa11bb"), interaction("b")], [verdict("a", "approve"), verdict("b", "approve")], [],
                   erased_subjects=["tok_00aa11bb"])
    assert [r["interaction_id"] for r in rows] == ["b"]


def test_custom_rules_with_their_own_pin_run(monkeypatch):
    rules = LabelRules(version="label_rules@test", weights={"approve": 1.0, "fix": 2.0, "reject": 0.0, "escalate": 0.0},
                       eval_only=("reject", "escalate"), late_outcome_overrides=False)
    monkeypatch.setitem(PINNED, "label_rules@test", rules.sha256)
    r = one(resolve([interaction("a")], [verdict("a", "approve")], [outcome("a", {"final_route": "siu"})], rules))
    assert r["verdict"]["route"] == "standard"


# ---------------------------------------------------------------- the 2k demo fixture


def test_fixture_counts_and_late_outcomes_recomputed_by_hand():
    ints, vds, ocs = events("interactions"), events("verdicts"), events("outcomes")
    # The generator's reviewers rarely miss, so this sample has no outcome that disagrees with its label. Make 40
    # late outcomes disagree (a system of record that corrects an approved route), then recompute by hand.
    approved = {v["interaction_id"] for v in vds if v["action"] == "approve"}
    routes = ["fast_track", "standard", "complex", "siu"]
    changed = 0
    for o in ocs:
        if o["interaction_id"] in approved and changed < 40:
            o["outcome"]["final_route"] = routes[(routes.index(o["outcome"]["final_route"]) + 1) % 4]
            changed += 1
    rows = resolve(ints, vds, ocs, output_schema=output_schema())  # no cutoff: every outcome is seen
    s = summary(rows)
    assert s["rows"] == 2000 and s["quarantined"] == []
    assert s["action"] == {"approve": 1738, "escalate": 26, "fix": 236}
    # independent recomputation: one verdict and at most one outcome per interaction in this fixture
    v_by = {v["interaction_id"]: v for v in vds}
    o_by = {o["interaction_id"]: o for o in ocs}
    i_by = {i["interaction_id"]: i for i in ints}
    overrides = 0
    for r in rows:
        v, o = v_by[r["interaction_id"]], o_by.get(r["interaction_id"])
        if v["action"] in ("reject", "escalate"):
            assert r["verdict"] is None
            continue
        base = dict(v["verdict"]) if v["action"] == "fix" else dict(i_by[r["interaction_id"]]["output"])
        if o is not None and o["observed_at"] >= v["ts"]:
            if base["route"] != o["outcome"]["final_route"]:
                overrides += 1
            base["route"] = o["outcome"]["final_route"]
        assert r["verdict"] == base
    assert overrides == 40
    assert s["label_source"]["outcome"] == overrides


def test_fixture_at_the_cutoff_sees_no_future_outcome():
    rows = resolve(events("interactions"), events("verdicts"), events("outcomes"), as_of=AS_OF)
    seen = {o["interaction_id"] for o in events("outcomes") if o["observed_at"] <= AS_OF}
    assert {r["interaction_id"] for r in rows if r["outcome"] is not None} == seen


def test_an_outcome_appended_twice_resolves_to_one_row():
    """reconcile on the same CSV twice appends the same outcome rows again; the build keeps one row per interaction."""
    oc = outcome("i1", {"route": "complex"}, oid="oc-1")
    once = resolve([interaction("i1")], [verdict("i1", "approve")], [oc], as_of=AS_OF, output_schema=output_schema())
    twice = resolve([interaction("i1")], [verdict("i1", "approve")], [oc, dict(oc)], as_of=AS_OF, output_schema=output_schema())
    assert len(twice) == 1 and twice == once
