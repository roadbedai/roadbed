# Rebase #13: claims-triage → Qwen/Qwen3.8-4B (SYNTHETIC)

Rebase #13 moves `claims-triage` for Acme Mutual (SYNTHETIC) from `Qwen/Qwen3.6-4B` to `Qwen/Qwen3.8-4B` @ `1d4bf0f2ff60`. Both models were scored on 60 sealed holdout cases (holdout `H-2026-11-01`). The candidate scores 100.0 against the incumbent's 90.0, a delta of +10.0 (95% CI +5.0 to +16.0). The gate passed. No slice is below the margin. 8 slice(s) have fewer than 30 rows and are flagged low-n; they never block.

> **Illustrative: predictions supplied, no training in this run.** The prediction files are synthetic fixtures passed with `--from-predictions`. The scoring, statistics, gate and hashes are the real code paths. All data in this repo is synthetic.

## Results

| Slice | n | Incumbent | Candidate | Delta | 95% CI | Flag |
|---|---:|---:|---:|---:|---|---|
| **overall** | 60 | 90.0 | 100.0 | +10.0 | +5.0 to +16.0 | |
| line=auto_physical | 15 | 80.0 | 100.0 | +20.0 | +8.0 to +36.0 | low-n |
| line=bodily_injury | 30 | 90.0 | 100.0 | +10.0 | +2.0 to +18.0 |  |
| line=property | 15 | 100.0 | 100.0 | 0.0 | 0.0 to 0.0 | low-n |
| state=CA | 20 | 70.0 | 100.0 | +30.0 | +18.0 to +42.0 | low-n |
| state=FL | 20 | 100.0 | 100.0 | 0.0 | 0.0 to 0.0 | low-n |
| state=TX | 20 | 100.0 | 100.0 | 0.0 | 0.0 to 0.0 | low-n |
| channel=broker | 21 | 88.6 | 100.0 | +11.4 | +2.9 to +22.9 | low-n |
| channel=direct | 21 | 91.4 | 100.0 | +8.6 | 0.0 to +17.1 | low-n |
| channel=partner | 18 | 90.0 | 100.0 | +10.0 | 0.0 to +20.0 | low-n |

## Gate checks

| Check | Status | Detail |
|---|---|---|
| slice_margin | pass | 0 of 9 slices below -2.0 margin; 8 flagged low-n |
| license | pass | apache-2.0 allowed by policy/license.yaml |
| contamination | skipped | train manifest absent: skipped |
| stage:shadow | skipped | stub: not run in prototype |
| stage:canary | skipped | stub: not run in prototype |
| stage:bias_tests | skipped | stub: not run in prototype |

Policy sha256 `4df02b116b2bdddd9be8b0c6c0c79d690fd7d0990d78b6ab093ccddea13a8321` · seed 20261101 · 10,000 bootstrap resamples. Run `roadbed verify rebases/13` to recompute every number.

opened rebase #13 → awaiting MRM sign-off
