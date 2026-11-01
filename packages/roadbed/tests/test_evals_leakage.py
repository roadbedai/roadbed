"""W14 leakage probes: near copies, shared inputs, eval-only rows and random data (property test)."""

from __future__ import annotations

import copy
import random

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_evals_support import AS_OF, SEED, events, input_ref, interaction, output_schema, slices, verdict

from roadbed.asset import jaccard, shingles
from roadbed.evals import EvalPolicy, build
from roadbed.labels import resolve


def _near_copies(n: int, rng: random.Random) -> tuple[list[dict], list[dict]]:
    """The fixture events plus n near copies: same claim text with one word added, new id and new input_ref."""
    ints, vds = events("interactions"), events("verdicts")
    v_by = {v["interaction_id"]: v for v in vds}
    for src in rng.sample(ints, n):
        c = copy.deepcopy(src)
        c["interaction_id"] = src["interaction_id"] + "-copy"
        c["input"]["narrative"] += " Resubmitted."
        c["input_ref"] = input_ref(c["input"])
        ints.append(c)
        v = dict(v_by[src["interaction_id"]], verdict_id=v_by[src["interaction_id"]]["verdict_id"] + "-copy", interaction_id=c["interaction_id"])
        vds.append(v)
    return ints, vds


def _visible(out) -> dict[str, set[str]]:
    return {
        "train": {r["interaction_id"] for r in out.snapshot.rows},
        "golden": {r["id"] for r in out.golden},
        "eval_only": {r["interaction_id"] for r in out.eval_only},
        "cts": {r["interaction_id"] for r in out.correction_tests},
    }


def test_near_copies_of_holdout_rows_are_purged_not_trained_on():
    rng = random.Random(11)
    ints, vds = _near_copies(300, rng)
    rows = resolve(ints, vds, [], as_of=AS_OF, output_schema=output_schema())
    out = build(rows, slices(), EvalPolicy(), SEED, as_of=AS_OF, output_schema=output_schema())
    hold = {r["id"] for r in out.holdout_candidate.rows}
    crossed = [i for i in hold if i.endswith("-copy") or i + "-copy" in {r["interaction_id"] for r in rows}]
    assert crossed, "the probe needs copies whose twin landed in the holdout"
    vis = _visible(out)
    for h in crossed:
        twin = h[: -len("-copy")] if h.endswith("-copy") else h + "-copy"
        assert all(twin not in s for s in vis.values()), (h, twin)
    assert out.report["purged"]["train"] + out.report["purged"]["golden"] >= len(crossed)
    assert out.proof["pairs"]["train-holdout"]["max_jaccard"] < 0.6


def test_rows_sharing_an_input_are_never_held_out():
    ints = [interaction(f"i{k}", narrative="same text for both claims about a cracked windshield on the highway") for k in range(2)]
    ints += [interaction(f"u{k:03d}", narrative=f"unique claim {k} " + " ".join(f"w{k}x{j}" for j in range(12))) for k in range(60)]
    vds = [verdict(i["interaction_id"], "approve") for i in ints]
    out = build(resolve(ints, vds, []), slices(), EvalPolicy(), 1, as_of=AS_OF, output_schema=output_schema())
    assert {"i0", "i1"} <= {r["interaction_id"] for r in out.snapshot.rows}
    assert out.report["counts"]["shared_input_rows"] == 2


def test_an_escalation_with_a_holdout_input_is_withheld_from_eval_only():
    ints = [interaction(f"u{k:03d}", narrative=f"unique claim {k} " + " ".join(f"w{k}x{j}" for j in range(12))) for k in range(80)]
    vds = [verdict(i["interaction_id"], "approve") for i in ints]
    base = build(resolve(ints, vds, []), slices(), EvalPolicy(), 2, as_of=AS_OF, output_schema=output_schema())
    held = base.holdout_candidate.rows[0]["id"]
    twin = next(i for i in ints if i["interaction_id"] == held)
    esc = dict(copy.deepcopy(twin), interaction_id="esc")
    out = build(resolve(ints + [esc], vds + [verdict("esc", "escalate")], []), slices(), EvalPolicy(), 2, as_of=AS_OF,
                output_schema=output_schema())
    assert held in {r["id"] for r in out.holdout_candidate.rows}
    assert "esc" not in {r["interaction_id"] for r in out.eval_only} and out.report["purged"]["eval_only"] == 1


def test_a_later_outcome_on_a_sealed_row_does_not_bring_it_back():
    """The row hash changes when a late outcome arrives; the sealed row is still found by id and input."""
    ints, vds = events("interactions"), events("verdicts")
    out = build(resolve(ints, vds, [], as_of=AS_OF), slices(), EvalPolicy(), SEED, as_of=AS_OF, output_schema=output_schema())
    sealed = out.holdout_candidate.rows
    ocs = [{"outcome_id": f"late-{r['id']}", "interaction_id": r["id"], "outcome": {"final_route": "siu"}, "source": "csv",
            "observed_at": "2026-11-03T10:00:00Z"} for r in sealed]
    later = resolve(ints, vds, ocs, as_of="2026-11-04T09:00:00Z")
    changed = {r["interaction_id"] for r in later if r["interaction_id"] in {s["id"] for s in sealed}
               and r["row_hash"] not in {s["row_hash"] for s in sealed}}
    assert changed
    nxt = build(later, slices(), EvalPolicy(), SEED, as_of="2026-11-04T09:00:00Z", output_schema=output_schema(), sealed_holdout=sealed)
    vis = _visible(nxt)
    assert not changed & (vis["train"] | vis["golden"] | vis["cts"])


WORDS = [f"w{i}" for i in range(400)]


@st.composite
def datasets(draw):
    n = draw(st.integers(40, 160))
    rnd = random.Random(draw(st.integers(0, 2**32 - 1)))
    lines, states, channels = ("property", "auto_physical", "liability"), ("CA", "TX"), ("direct", "broker")
    ints, vds = [], []
    texts: list[str] = []
    for k in range(n):
        if texts and rnd.random() < 0.25:  # near or exact copy of an earlier text
            t = rnd.choice(texts) + ("" if rnd.random() < 0.3 else " again")
        else:
            t = " ".join(rnd.choice(WORDS) for _ in range(14))
        texts.append(t)
        ints.append(interaction(f"r{k:04d}", narrative=t, line=rnd.choice(lines), state=rnd.choice(states), channel=rnd.choice(channels)))
        action = rnd.choices(["approve", "fix", "escalate", "reject"], [70, 20, 5, 5])[0]
        vds.append(verdict(f"r{k:04d}", action, reason="bad_output" if action == "reject" else None))
    return ints, vds, draw(st.integers(0, 10_000))


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(datasets())
def test_property_train_and_holdout_are_disjoint(data):
    ints, vds, seed = data
    rows = resolve(ints, vds, [])
    try:
        out = build(rows, slices(), EvalPolicy(), seed, as_of=AS_OF, output_schema=output_schema())
    except ValueError as e:  # tiny draws can purge every training row; the build refuses rather than leak
        assert "no training rows" in str(e)
        return
    text = {r["interaction_id"]: r["input_text"] for r in rows}
    hold = out.holdout_candidate.rows
    hid = {r["id"] for r in hold}
    vis = _visible(out)
    assert not hid & (vis["train"] | vis["golden"] | vis["eval_only"] | vis["cts"])
    assert not {r["row_hash"] for r in out.snapshot.rows} & {r["row_hash"] for r in hold}
    assert not {r["input_ref"] for r in out.snapshot.rows} & {r["input_ref"] for r in hold}
    hs = [shingles(text[h]) for h in hid]
    for t in vis["train"] | vis["golden"] | vis["eval_only"]:
        st_ = shingles(text[t])
        assert all(jaccard(st_, h) < 0.6 for h in hs)
    for p in out.snapshot.parts:  # holdout rows never in the part bytes
        assert not any(h.encode() in p.data for h in hid)


def test_near_copies_of_rows_trained_before_are_never_held_out():
    ints, vds = _near_copies(300, random.Random(5))
    first = build(resolve(events("interactions"), events("verdicts"), []), slices(), EvalPolicy(), SEED, as_of=AS_OF,
                  output_schema=output_schema())
    trained = {r["interaction_id"] for r in first.snapshot.rows}
    copies_of_trained = {i["interaction_id"] for i in ints if i["interaction_id"].endswith("-copy") and i["interaction_id"][:-5] in trained}
    assert copies_of_trained
    nxt = build(resolve(ints, vds, []), slices(), EvalPolicy(), SEED, as_of=AS_OF, output_schema=output_schema(),
                trained_on=first.snapshot.rows, suffix="b")
    held = {r["id"] for r in nxt.holdout_candidate.rows} | {r["id"] for r in nxt.golden}
    assert not held & (trained | copies_of_trained)
    assert nxt.proof["pairs"]["holdout-trained_on"]["near_pairs"] == 0
