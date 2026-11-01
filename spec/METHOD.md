# Method: scores, confidence intervals, contamination and the gate

This file tells how Roadbed compares a candidate model with the incumbent model.
The code is in `packages/roadbed/src/roadbed/{scoring,stats,contamination,gate}.py`.
The tests that prove each claim are in `packages/roadbed/tests/`.
SPEC.md has the rules. This file gives the reasons in plain words.

## 1. Row score

Each holdout row has the expected output from a reviewer.
`evals/scorers.yaml` gives a weight to each output field.
The score of one row is 100 times the sum of the weights of the correct fields, divided by the sum of all weights.

Example with `route` 0.6 and `severity` 0.4:

| Output | Row score |
|---|---|
| Both fields correct | 100 |
| Only `route` correct | 60 |
| Only `severity` correct | 40 |
| Field missing | 0 for that field |

A field is correct only when its JSON value is the same as the expected value.
The number 1 and the number 1.0 are the same value.
The boolean `true` is not the number 1.

Tests: `test_scoring.py`.

## 2. Paired difference

Both models answer the same holdout rows.
For each row, we subtract the incumbent score from the candidate score.
The delta is the mean of these differences, in score points.
Because the two models use the same rows, the row difficulty cancels out.
This "paired" comparison gives narrower intervals than two separate means.

## 3. Confidence interval: the paired bootstrap

The delta comes from a sample of rows. A different sample gives a different delta.
The confidence interval (CI) shows how much the delta can change.
We use the percentile bootstrap:

1. Take the list of paired differences. It has n values.
2. Draw n values from the list at random, with replacement. Calculate their mean.
3. Do step 2 again, `n_boot` times. `policy/gate.yaml` sets `n_boot`, usually 10,000.
4. Sort the means. The 2.5% point and the 97.5% point are the 95% CI.

The CI level is `ci` in `policy/gate.yaml`, usually 0.95.

**Same result each time.** The random generator is numpy PCG64.
Its seed is `[seed, crc32("<key>=<value>")]` for a slice and `[seed, crc32("overall")]` for all rows.
`scores.json` stores the seed, so `roadbed verify` can calculate the same CI again.

Each slice has its own seed, so one slice does not change the CI of a different slice.
The engine sorts rows by id first, so the order of rows in a file does not change a number.

The engine draws the indices in blocks of 1,000 resamples as `rng.integers(0, n, size=(block, n))`.
It then uses `np.quantile` with linear interpolation.
This exact procedure is part of the contract. A change to it changes the stored CIs.

**Known answer.** Some cases have an exact answer that does not use the random generator.
The demo slice has n = 120 rows. 18 rows lose 60 points and the remaining rows lose 0. The delta is -9.0.

The number of copies of -60 in one resample is binomial with n = 120 and p = 0.15.
Its 2.5% point is 11 and its 97.5% point is 26.
Thus the exact CI is -60 x 26 / 120 = -13.00 to -60 x 11 / 120 = -5.50.

The engine gives -13.00 to -5.50 with the stored seed.
The engine gives the same answer for at least 37 of 40 other seeds.
Tests: `test_fixed_seed_ci_known_answer_to_2dp`, `test_binomial_known_answer_across_seeds`.

**Coverage.** A 95% CI must contain the true delta in about 95% of samples.
We drew 2,000 samples from a known distribution with a true delta of -6.0.
We used the real settings: 10,000 resamples and a 95% level.

| Rows per sample | Share of CIs that contain -6.0 |
|---|---|
| 120 | 0.954 |
| 30 (`min_n`) | 0.942 |

At 30 rows, the percentile bootstrap is a little too narrow.
This is a known property of the method on small samples.
Thus slices below `min_n` never block.
The test is `test_nominal_coverage_about_95`. It is marked `slow`.
The workflow `.github/workflows/roadbed-stats.yml` runs it on each pull request that changes the engine.

## 4. Slices, low-n and the margin

`evals/slices.yaml` lists the slice keys and the permitted values.
The engine calculates the delta and the CI for each value that occurs in the holdout.
It sorts the values in each key.
A holdout value that `slices.yaml` does not list stops the run with an error.

Two flags come from `policy/gate.yaml`:

- **low_n.** A slice with fewer than `min_n` rows (30) is `low_n`. A low-n slice never blocks. Its CI is too wide to support a decision.
- **below_margin.** A slice is `below_margin` when its delta is less than `margin` (-2.0) and it is not low-n. A delta of exactly -2.0 does not block. The engine ignores floating-point noise smaller than 1e-9.

The overall delta can look good while one slice is bad.
In the blocked fixture, the overall delta is -5.0, but `line=bodily_injury` is -20.0.
The gate looks at each slice for this reason.

## 5. Contamination

The holdout is valid only if the model did not train on it.
`roadbed.contamination.check` compares the training snapshot with the sealed holdout in two ways:

- **Exact overlap.** A row hash that is in both sets. Each shared row hash is a leaked row.
- **Near overlap.** Two raw input texts that are almost the same. We cut each text into word groups of 3 words ("shingles"). The Jaccard similarity is the number of shared shingles divided by the number of all distinct shingles. A pair with a similarity of 0.8 or more is a near-duplicate.

To compare large sets fast, we use MinHash.
For each of 128 slots, the slot value is the smallest hash of all shingles in the text.
The share of equal slots between two texts is an estimate of their Jaccard similarity.
The standard error of the estimate is about 0.035 at a similarity of 0.8.
Small sets can use the exact Jaccard similarity (`method: jaccard`).
The signature format is the same as the holdout MinHash object that `SEAL.json` names.

Each text must have the sha256 of its `input_ref`. If not, the check stops with an error.
A text with fewer than 3 words is one shingle.

The report also holds the seal order: the seal must come before the training run.
This check needs git, so the caller supplies it (W02).
The report has `status: pass` only when there is no shared row, no near pair, and the seal order is correct.

Tests: `test_contamination.py`, and `test_fixture_reproduction.py` for the fixtures.

## 6. The gate

`roadbed.gate.evaluate` makes the decision from the scores, the contamination report, the license decision and the policy.
It does these checks, in this order:

| Check | Fails when |
|---|---|
| `slice_margin` | One or more slices are `below_margin`. |
| `license` | The license decision is `deny` or `review`. A `review` decision needs a person, so the gate does not pass it. |
| `contamination` | `contamination.json` has `status: fail`, or its parts do not agree. |
| `stage:shadow`, `stage:canary`, `stage:bias_tests` | Never. These stages are stubs and are always `skipped`. |

A failed check blocks the rebase when its reason is in `block_on`. If not, its status is `warn`.
A missing input gives a `skipped` check with the reason.
Each below-margin slice becomes a finding, `F-0001`, `F-0002` and so on.

The gate stops with an error when the inputs do not agree:

- The scores used a different seed, `n_boot`, `ci`, `margin` or `min_n` than the policy.
- The contamination report or the license decision is for a different rebase.

The gate also does checks that JSON Schema cannot do.
For example, a report with `max_jaccard` at or above the threshold must list a near pair.
A report that fails these checks makes the contamination check fail.

## 7. What the tests prove

| Claim | Test |
|---|---|
| Identical predictions give a delta of 0 and a CI that contains 0 | `test_identical_predictions_give_zero_delta_and_ci_contains_zero` (Hypothesis) |
| Fixed-seed CI to 2 decimal places | `test_fixed_seed_ci_known_answer_to_2dp` |
| -9.0 at n = 120 blocks | `test_minus_nine_at_n120_blocks_and_logs_finding` |
| n = 12 is only flagged low-n | `test_n12_slice_only_flags_low_n_and_passes` |
| 3 leaked rows block | `test_three_leaked_rows_block`, `test_three_leaked_rows_fail_and_block` |
| Coverage is about 95% | `test_nominal_coverage_about_95` (slow) |
| The fixtures reproduce byte for byte | `test_scores_and_gate_reproduce_byte_for_byte` |

The fixtures in `spec/examples` are SYNTHETIC. Their receipts say `executor: mock`.
They prove that the engine is correct. They do not prove anything about a model.
