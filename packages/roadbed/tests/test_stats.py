from __future__ import annotations

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from conftest import SCORERS, holdout_row, make_policy, pred, wrong_route
from roadbed.stats import bootstrap_ci, compare

KW = dict(scorers=SCORERS, synthetic=True, model="claims-triage", rebase=13)


def _rows(n, start=0, **slices):
    return [holdout_row(start + i, **slices) for i in range(n)]


def test_identical_predictions_give_zero_delta(policy):
    hold = _rows(50, line="property")
    preds = [pred(r) for r in hold]
    s = compare(hold, preds, preds, ["line"], policy, **KW)
    assert s.overall.delta == 0.0 and s.overall.ci_low <= 0.0 <= s.overall.ci_high
    assert s.slices[0].delta == 0.0 and not s.slices[0].below_margin


def test_hand_computed_small_table(policy):
    # incumbent all correct (100 each); candidate: route wrong (40), severity wrong (60), right, right
    hold = _rows(4, line="property")
    inc = [pred(r) for r in hold]
    cand = [
        pred(hold[0], wrong_route(hold[0]["expected"])),
        pred(hold[1], {**hold[1]["expected"], "severity": 9}),
        pred(hold[2]),
        pred(hold[3]),
    ]
    s = compare(hold, inc, cand, ["line"], policy, **KW)
    # diffs: -60, -40, 0, 0 -> mean -25
    assert s.overall.incumbent == 100.0
    assert s.overall.candidate == pytest.approx(75.0)
    assert s.overall.delta == pytest.approx(-25.0)
    assert -60.0 <= s.overall.ci_low <= s.overall.delta <= s.overall.ci_high <= 0.0


def test_fixed_seed_is_reproducible():
    hold = _rows(200, line="liability")
    inc = [pred(r) for r in hold]
    cand = [pred(r, wrong_route(r["expected"])) if i % 7 == 0 else pred(r) for i, r in enumerate(hold)]
    pol = make_policy(n_boot=10000)
    a = compare(hold, inc, cand, ["line"], pol, **KW)
    b = compare(list(reversed(hold)), inc, cand, ["line"], pol, **KW)
    assert round(a.overall.ci_low, 6) == round(b.overall.ci_low, 6)
    assert round(a.overall.ci_high, 6) == round(b.overall.ci_high, 6)
    c = compare(hold, inc, cand, ["line"], pol, **KW)
    assert c == a


def test_slice_ci_independent_of_other_slices(policy):
    bi = _rows(60, line="bodily_injury")
    other = _rows(40, start=1000, line="property")
    cand_fn = lambda rows: [pred(r, wrong_route(r["expected"])) if i % 5 == 0 else pred(r) for i, r in enumerate(rows)]
    alone = compare(bi, [pred(r) for r in bi], cand_fn(bi), ["line"], policy, **KW).slices[0]
    both = compare(bi + other, [pred(r) for r in bi + other], cand_fn(bi) + cand_fn(other), ["line"], policy, **KW)
    s = next(x for x in both.slices if x.value == "bodily_injury")
    assert (s.ci_low, s.ci_high) == (alone.ci_low, alone.ci_high)


def test_minus_nine_at_n120_is_below_margin(policy):
    hold = _rows(120, line="bodily_injury")
    inc = [pred(r) for r in hold]
    # 18 of 120 rows lose 60 points: delta = -1080 / 120 = -9.0
    cand = [pred(r, wrong_route(r["expected"])) if i < 18 else pred(r) for i, r in enumerate(hold)]
    s = compare(hold, inc, cand, ["line"], policy, **KW).slices[0]
    assert s.n == 120 and s.delta == pytest.approx(-9.0)
    assert s.below_margin and not s.low_n
    assert s.ci_high < 0


def test_n12_only_flags_low_n(policy):
    hold = _rows(12, state="IL")
    inc = [pred(r) for r in hold]
    cand = [pred(r, wrong_route(r["expected"])) for r in hold]
    s = compare(hold, inc, cand, ["state"], policy, **KW).slices[0]
    assert s.n == 12 and s.delta == pytest.approx(-60.0)
    assert s.low_n and not s.below_margin


def test_slices_ordering_and_counts(policy):
    hold = _rows(5, line="b", state="TX") + _rows(5, start=10, line="a", state="CA")
    preds = [pred(r) for r in hold]
    s = compare(hold, preds, preds, ["line", "state"], policy, **KW)
    assert [(x.key, x.value, x.n) for x in s.slices] == [
        ("line", "a", 5), ("line", "b", 5), ("state", "CA", 5), ("state", "TX", 5)
    ]


def test_default_digests_and_metadata(policy):
    hold = _rows(3, line="a")
    preds = [pred(r) for r in hold]
    s = compare(hold, preds, preds, ["line"], policy, notice="no training in this run", **KW)
    assert s.spec == "roadbed/v0.1" and s.synthetic and s.rebase == 13
    assert s.holdout.rows == 3 and s.holdout.id == "inline" and len(s.holdout.sha256) == 64
    assert s.seed == policy.seed and s.n_boot == policy.n_boot and s.ci == policy.ci


@pytest.mark.parametrize(
    "mutate,msg",
    [
        (lambda inc: inc[:-1], "do not match"),
        (lambda inc: inc + [inc[0]], "duplicate"),
    ],
)
def test_mismatched_predictions_raise(policy, mutate, msg):
    hold = _rows(5, line="a")
    preds = [pred(r) for r in hold]
    with pytest.raises(ValueError, match=msg):
        compare(hold, mutate(preds), preds, ["line"], policy, **KW)


def test_empty_holdout_raises(policy):
    with pytest.raises(ValueError, match="empty"):
        compare([], [], [], ["line"], policy, **KW)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    diffs=st.lists(st.sampled_from([-100.0, -60.0, -40.0, 0.0, 40.0, 60.0, 100.0]), min_size=1, max_size=150),
    seed=st.integers(0, 2**32 - 1),
)
def test_ci_contains_point_delta(diffs, seed):
    d = np.array(diffs)
    lo, hi = bootstrap_ci(d, n_boot=1000, ci=0.95, seed=seed, stream="prop")
    assert lo <= d.mean() + 1e-9 and d.mean() - 1e-9 <= hi


@pytest.mark.slow
def test_nominal_coverage_about_95():
    """Paired diffs from a known distribution: the 95% CI should cover the true mean about 95% of the time."""
    rng = np.random.default_rng(7)
    values = np.array([-60.0, -40.0, 0.0, 40.0, 60.0])
    probs = np.array([0.08, 0.07, 0.7, 0.08, 0.07])
    true_mean = float(values @ probs)
    reps, covered = 400, 0
    for r in range(reps):
        d = rng.choice(values, size=300, p=probs)
        lo, hi = bootstrap_ci(d, n_boot=2000, ci=0.95, seed=r, stream="cov")
        covered += lo <= true_mean <= hi
    assert 0.92 <= covered / reps <= 0.98
