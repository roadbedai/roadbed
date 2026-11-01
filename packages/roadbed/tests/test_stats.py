"""roadbed.stats: paired bootstrap per slice. Every numeric claim here has a known answer worked out by hand
or from the exact bootstrap distribution, never from the engine's own output."""

from __future__ import annotations

from math import comb

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from conftest import ROUTES, SCORERS, holdout_row, make_policy, pred, wrong_route
from roadbed.contracts import Slices
from roadbed.stats import MARGIN_EPS, bootstrap_ci, compare

KW = dict(scorers=SCORERS, synthetic=True, model="claims-triage", rebase=13)


def _rows(n, start=0, **slices):
    return [holdout_row(start + i, **slices) for i in range(n)]


def _minus_nine(n=120, k=18):
    """n rows; the candidate gets the route wrong (row score 40, diff -60) on the first k: delta = -60k/n."""
    hold = _rows(n, line="bodily_injury")
    inc = [pred(r) for r in hold]
    cand = [pred(r, wrong_route(r["expected"])) if i < k else pred(r) for i, r in enumerate(hold)]
    return hold, inc, cand


# ---------------------------------------------------------------- identical predictions (Hypothesis)

row_outputs = st.fixed_dictionaries({"route": st.sampled_from(ROUTES), "severity": st.integers(1, 5)})


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    outputs=st.lists(row_outputs, min_size=1, max_size=80),
    lines=st.lists(st.sampled_from(["auto_physical", "bodily_injury", "property"]), min_size=80, max_size=80),
    seed=st.integers(0, 2**32 - 1),
)
def test_identical_predictions_give_zero_delta_and_ci_contains_zero(outputs, lines, seed):
    hold = [holdout_row(i, line=lines[i]) for i in range(len(outputs))]
    preds = [pred(r, o) for r, o in zip(hold, outputs)]  # any outputs, right or wrong, the same for both
    s = compare(preds, preds, hold, ["line"], make_policy(seed=seed, n_boot=200), **KW)
    for x in [s.overall, *s.slices]:
        assert x.delta == 0.0 and x.ci_low == 0.0 and x.ci_high == 0.0
        assert x.incumbent == x.candidate
    assert not any(x.below_margin for x in s.slices)
    assert sum(x.n for x in s.slices) == s.overall.n == len(hold)


# ---------------------------------------------------------------- hand-computed values


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
    s = compare(inc, cand, hold, ["line"], policy, **KW)
    # diffs: -60, -40, 0, 0 -> mean -25
    assert s.overall.incumbent == 100.0
    assert s.overall.candidate == pytest.approx(75.0)
    assert s.overall.delta == pytest.approx(-25.0)
    assert -60.0 <= s.overall.ci_low <= s.overall.delta <= s.overall.ci_high <= 0.0


@pytest.mark.parametrize("diffs,expected", [([-25.0], (-25.0, -25.0)), ([7.5] * 40, (7.5, 7.5))])
def test_degenerate_bootstrap_known_answer(diffs, expected):
    """One row, or all rows equal: every resample mean is that value, so the CI is the point."""
    assert bootstrap_ci(np.array(diffs), n_boot=10000, ci=0.95, seed=1, stream="x") == expected


def test_two_point_bootstrap_known_answer():
    """diffs [0, 100]: resample means are 0, 50, 100 with p 1/4, 1/2, 1/4. The 2.5% and 97.5% quantiles are 0 and 100."""
    assert bootstrap_ci(np.array([0.0, 100.0]), n_boot=10000, ci=0.95, seed=3, stream="x") == (0.0, 100.0)


def _binomial_ci(n: int, k: int, value: float, ci: float = 0.95) -> tuple[float, float]:
    """Exact percentile CI of the bootstrap mean when k of n diffs are `value` (< 0) and the rest are 0.

    A resample holds B ~ Binomial(n, k/n) copies of `value`, so its mean is value * B / n. The q-quantile of B
    is the smallest b with P(B <= b) > q, computed with exact binomial terms (no RNG involved).
    """
    p = k / n
    cdf, c = [], 0.0
    for b in range(n + 1):
        c += comb(n, b) * p**b * (1 - p) ** (n - b)
        cdf.append(c)
    alpha = (1 - ci) / 2
    b_lo = next(b for b in range(n + 1) if cdf[b] > alpha)
    b_hi = next(b for b in range(n + 1) if cdf[b] > 1 - alpha)
    return value * b_hi / n, value * b_lo / n  # value < 0: more copies is a lower mean


def test_fixed_seed_ci_known_answer_to_2dp():
    """The demo case: line=bodily_injury, n=120, delta -9.0, seed 20261101, 10,000 resamples.

    Exact answer: B ~ Binomial(120, 0.15) has 2.5% quantile 11 and 97.5% quantile 26, so the CI is
    -60 * 26 / 120 = -13.00 to -60 * 11 / 120 = -5.50.
    """
    assert _binomial_ci(120, 18, -60.0) == (-13.0, -5.5)
    hold, inc, cand = _minus_nine()
    s = compare(inc, cand, hold, ["line"], make_policy(n_boot=10000, seed=20261101), **KW).slices[0]
    assert s.n == 120 and round(s.delta, 2) == -9.00
    assert (round(s.ci_low, 2), round(s.ci_high, 2)) == (-13.00, -5.50)


@pytest.mark.parametrize("n,k", [(120, 18), (60, 2), (100, 13)])
def test_binomial_known_answer_across_seeds(n, k):
    """The fixed seed is not a lucky pick: over 40 seeds the engine's CI equals the exact answer almost always.

    The empirical quantile can miss its atom only when the resample count below it lands within a few
    standard deviations of the 2.5% cut. For (120, 18) that margin is about 2.3 sd (about 1% of seeds miss).
    """
    expected = _binomial_ci(n, k, -60.0)
    d = np.array([-60.0] * k + [0.0] * (n - k))
    hits = sum(bootstrap_ci(d, n_boot=10000, ci=0.95, seed=s, stream="known") == pytest.approx(expected) for s in range(40))
    assert hits >= 37


def test_fixed_seed_is_reproducible_and_row_order_free():
    hold = _rows(200, line="liability")
    inc = [pred(r) for r in hold]
    cand = [pred(r, wrong_route(r["expected"])) if i % 7 == 0 else pred(r) for i, r in enumerate(hold)]
    pol = make_policy(n_boot=10000)
    a = compare(inc, cand, hold, ["line"], pol, **KW)
    b = compare(list(reversed(inc)), cand, list(reversed(hold)), ["line"], pol, **KW)
    assert (a.overall.ci_low, a.overall.ci_high) == (b.overall.ci_low, b.overall.ci_high)
    assert compare(inc, cand, hold, ["line"], pol, **KW) == a
    # the seed and the stream name do drive the draws (diffs with many distinct values, so quantiles move)
    d = np.arange(50.0)
    ref = bootstrap_ci(d, n_boot=2000, ci=0.95, seed=0, stream="overall")
    assert bootstrap_ci(d, n_boot=2000, ci=0.95, seed=1, stream="overall") != ref
    assert bootstrap_ci(d, n_boot=2000, ci=0.95, seed=0, stream="line=a") != ref


def test_slice_ci_independent_of_other_slices(policy):
    bi = _rows(60, line="bodily_injury")
    other = _rows(40, start=1000, line="property")
    cand_fn = lambda rows: [pred(r, wrong_route(r["expected"])) if i % 5 == 0 else pred(r) for i, r in enumerate(rows)]
    alone = compare([pred(r) for r in bi], cand_fn(bi), bi, ["line"], policy, **KW).slices[0]
    both = compare([pred(r) for r in bi + other], cand_fn(bi) + cand_fn(other), bi + other, ["line"], policy, **KW)
    s = next(x for x in both.slices if x.value == "bodily_injury")
    assert (s.ci_low, s.ci_high) == (alone.ci_low, alone.ci_high)


# ---------------------------------------------------------------- low-n and margin flags


def test_minus_nine_at_n120_is_below_margin(policy):
    hold, inc, cand = _minus_nine()
    s = compare(inc, cand, hold, ["line"], policy, **KW).slices[0]
    assert s.n == 120 and s.delta == pytest.approx(-9.0)  # 18 * -60 / 120
    assert s.below_margin and not s.low_n
    assert s.ci_high < 0


def test_n12_only_flags_low_n(policy):
    hold = _rows(12, state="IL")
    inc = [pred(r) for r in hold]
    cand = [pred(r, wrong_route(r["expected"])) for r in hold]
    s = compare(inc, cand, hold, ["state"], policy, **KW).slices[0]
    assert s.n == 12 and s.delta == pytest.approx(-60.0)
    assert s.low_n and not s.below_margin


@pytest.mark.parametrize("n,low_n", [(29, True), (30, False)])
def test_min_n_boundary(policy, n, low_n):
    hold = _rows(n, line="a")
    s = compare([pred(r) for r in hold], [pred(r, wrong_route(r["expected"])) for r in hold], hold, ["line"], policy, **KW)
    assert s.slices[0].low_n is low_n and s.slices[0].below_margin is not low_n


def test_margin_boundary_is_exclusive_and_ignores_float_noise():
    """delta exactly at the margin does not block; a hair below it (beyond float noise) does."""
    hold = _rows(30, line="a")
    inc = [pred(r) for r in hold]
    cand = [pred(r, wrong_route(r["expected"])) if i == 0 else pred(r) for i, r in enumerate(hold)]  # delta -60/30 = -2.0
    at = compare(inc, cand, hold, ["line"], make_policy(margin=-2.0), **KW).slices[0]
    assert at.delta == pytest.approx(-2.0, abs=1e-12) and not at.below_margin
    under = compare(inc, cand, hold, ["line"], make_policy(margin=-2.0 + 10 * MARGIN_EPS), **KW).slices[0]
    assert under.below_margin


def test_slices_ordering_and_counts(policy):
    hold = _rows(5, line="b", state="TX") + _rows(5, start=10, line="a", state="CA")
    preds = [pred(r) for r in hold]
    s = compare(preds, preds, hold, ["line", "state"], policy, **KW)
    assert [(x.key, x.value, x.n) for x in s.slices] == [
        ("line", "a", 5), ("line", "b", 5), ("state", "CA", 5), ("state", "TX", 5)
    ]


def test_slices_model_rejects_undeclared_value(policy):
    hold = _rows(3, line="a") + _rows(2, start=10, line="zzz")
    preds = [pred(r) for r in hold]
    declared = Slices(keys={"line": ["b", "a"]})
    with pytest.raises(ValueError, match="slices.yaml does not list"):
        compare(preds, preds, hold, declared, policy, **KW)
    ok = compare(preds[:3], preds[:3], hold[:3], declared, policy, **KW)
    assert [(x.key, x.value) for x in ok.slices] == [("line", "a")]


def test_row_without_slice_key_is_in_no_slice_of_that_key(policy):
    hold = _rows(3, line="a") + _rows(2, start=10)
    preds = [pred(r) for r in hold]
    s = compare(preds, preds, hold, ["line"], policy, **KW)
    assert s.overall.n == 5 and [(x.value, x.n) for x in s.slices] == [("a", 3)]


# ---------------------------------------------------------------- metadata and input errors


def test_default_digests_and_metadata(policy):
    hold = _rows(3, line="a")
    preds = [pred(r) for r in hold]
    s = compare(preds, preds, hold, ["line"], policy, notice="no training in this run", **KW)
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
        compare(mutate(preds), preds, hold, ["line"], policy, **KW)


def test_empty_holdout_raises(policy):
    with pytest.raises(ValueError, match="empty"):
        compare([], [], [], ["line"], policy, **KW)
    with pytest.raises(ValueError, match="non-empty"):
        bootstrap_ci(np.array([]), n_boot=100, ci=0.95, seed=0, stream="x")


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    diffs=st.lists(st.sampled_from([-100.0, -60.0, -40.0, 0.0, 40.0, 60.0, 100.0]), min_size=1, max_size=150),
    seed=st.integers(0, 2**32 - 1),
)
def test_ci_contains_point_delta(diffs, seed):
    d = np.array(diffs)
    lo, hi = bootstrap_ci(d, n_boot=1000, ci=0.95, seed=seed, stream="prop")
    assert d.min() <= lo <= d.mean() + 1e-9 and d.mean() - 1e-9 <= hi <= d.max()


# ---------------------------------------------------------------- coverage (slow; CI runs it in roadbed-stats.yml)


@pytest.mark.slow
@pytest.mark.parametrize("n,band", [(120, (0.935, 0.965)), (30, (0.92, 0.965))])
def test_nominal_coverage_about_95(n, band):
    """Draw paired diffs from a known distribution (true mean -6.0) 2,000 times with the real settings
    (10,000 resamples, 95%). The CI should cover -6.0 about 95% of the time. 2,000 reps give a standard
    error of about 0.005. At n=30 (min_n) the percentile bootstrap covers a little less (measured 0.942)."""
    values = np.array([-60.0, -40.0, 0.0, 40.0, 60.0])
    probs = np.array([0.15, 0.05, 0.70, 0.05, 0.05])
    true_mean = float(values @ probs)
    assert true_mean == pytest.approx(-6.0)
    rng = np.random.default_rng(n)
    reps, covered = 2000, 0
    for r in range(reps):
        d = rng.choice(values, size=n, p=probs)
        lo, hi = bootstrap_ci(d, n_boot=10000, ci=0.95, seed=r, stream="cov")
        covered += lo <= true_mean <= hi
    assert band[0] <= covered / reps <= band[1]
