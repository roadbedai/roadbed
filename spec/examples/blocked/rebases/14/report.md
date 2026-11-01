# Rebase #14: claims-triage → Qwen/Qwen3.8-4B (SYNTHETIC)

Rebase #14 moves `claims-triage` for Acme Mutual (SYNTHETIC) from `Qwen/Qwen3.6-4B` to `Qwen/Qwen3.8-4B` @ `1d4bf0f2ff60`. Both models were scored on 60 sealed holdout cases (holdout `H-2026-11-01`). The candidate scores 85.0 against the incumbent's 90.0, a delta of −5.0 (95% CI −13.0 to +3.0). The gate blocked this rebase: 1 slice(s) fell below the −2.0 margin. 8 slice(s) have fewer than 30 rows and are flagged low-n; they never block.

> **Illustrative: predictions supplied, no training in this run.** The prediction files are synthetic fixtures passed with `--from-predictions`. The scoring, statistics, gate and hashes are the real code paths. All data in this repo is synthetic.

## Results

| Slice | n | Incumbent | Candidate | Delta | 95% CI | Flag |
|---|---:|---:|---:|---:|---|---|
| **overall** | 60 | 90.0 | 85.0 | −5.0 | −13.0 to +3.0 | |
| line=auto_physical | 15 | 80.0 | 100.0 | +20.0 | +8.0 to +36.0 | low-n |
| line=bodily_injury | 30 | 90.0 | 70.0 | −20.0 | −32.0 to −8.0 | below margin |
| line=property | 15 | 100.0 | 100.0 | 0.0 | 0.0 to 0.0 | low-n |
| state=CA | 20 | 70.0 | 85.0 | +15.0 | −0.1 to +30.0 | low-n |
| state=FL | 20 | 100.0 | 85.0 | −15.0 | −27.0 to −6.0 | low-n |
| state=TX | 20 | 100.0 | 85.0 | −15.0 | −27.0 to −6.0 | low-n |
| channel=broker | 21 | 88.6 | 82.9 | −5.7 | −20.0 to +8.6 | low-n |
| channel=direct | 21 | 91.4 | 82.9 | −8.6 | −22.9 to +5.7 | low-n |
| channel=partner | 18 | 90.0 | 90.0 | 0.0 | −13.3 to +13.3 | low-n |

## Gate checks

| Check | Status | Detail |
|---|---|---|
| slice_margin | fail | 1 of 9 slices below -2.0 margin (line=bodily_injury -20.0); 8 flagged low-n |
| license | pass | apache-2.0 allowed by policy/license.yaml |
| contamination | skipped | train manifest absent: skipped |
| stage:shadow | skipped | stub: not run in prototype |
| stage:canary | skipped | stub: not run in prototype |
| stage:bias_tests | skipped | stub: not run in prototype |

## Findings

- F-0001: line=bodily_injury −20.0 (CI −32.0 to −8.0, n=30) below −2.0 margin

Policy sha256 `4df02b116b2bdddd9be8b0c6c0c79d690fd7d0990d78b6ab093ccddea13a8321` · seed 20261101 · 10,000 bootstrap resamples. Run `roadbed verify rebases/14` to recompute every number.

blocked: line=bodily_injury −20.0 below −2.0 margin · F-0001 logged
