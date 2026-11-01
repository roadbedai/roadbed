# Roadbed asset format, v0.1

Status: prototype draft. Spec id: `roadbed/v0.1`.

An asset repo holds everything Roadbed needs to rebase one fine-tuned model onto a new base model, score it, and gate it.
The JSON Schemas (Draft 2020-12) are in `schemas/v0.1/`. They are generated from `roadbed.contracts` and a test keeps them in sync.
A complete synthetic example is in `examples/claims-triage/`.

## Files

| Path (from asset root) | Schema | Purpose |
|---|---|---|
| `roadbed.yaml` | `roadbed.schema.json` | Root manifest: model, tenant, task, base model repo and revision, holdout, evals, recipe, gate policy, serving alias. |
| `evals/output.schema.json` | (tenant-defined) | JSON Schema of one model output and one reviewer verdict. |
| `evals/scorers.yaml` | `scorers.schema.json` | How to score one row: weighted exact match of output fields, 0 to 100. |
| `evals/slices.yaml` | `slices.schema.json` | Slice keys and their values. Each value of each key is one slice. |
| `data/holdout/<id>/holdout.jsonl` | `holdout-row.schema.json` | Sealed holdout. One row per line: id, slice values, input content hash, reviewer verdict. |
| `policy/gate.yaml` | `gate-policy.schema.json` | Gate policy: margin, min_n, CI level, bootstrap size and seed, what blocks, stage stubs. |
| `policy/license.yaml` | `license-policy.schema.json` | Allowed base-model licenses (lowercase SPDX ids). |
| `recipes/...` | (none in v0.1) | Training recipe. May be a stub. |
| `rebases/<n>/predictions.incumbent.jsonl` | `prediction-row.schema.json` | Outputs of the current production model on the holdout. |
| `rebases/<n>/predictions.candidate.jsonl` | `prediction-row.schema.json` | Outputs of the rebased candidate on the holdout. |
| `rebases/<n>/scores.json` | `scores.schema.json` | Overall and per-slice scores, deltas and bootstrap CIs. Includes hashes of the holdout and prediction files. |
| `rebases/<n>/gate.json` | `gate.schema.json` | Gate result: pass or blocked, the checks, and findings for below-margin slices. |
| `rebases/<n>/report.md` | (free text) | Pull request body made from scores.json and gate.json. |
| `findings/F-NNNN.*` | (free text in v0.1) | Logged findings. Ids are F-0001, F-0002, ... in order. |

## Rules

1. Row score = 100 x sum(weight x exact match) / sum(weight). A missing field scores 0.
2. Delta = candidate mean - incumbent mean, in score points, on the same rows (paired).
3. CI = percentile bootstrap of the mean paired difference, `n_boot` resamples, level `ci`. The RNG seed is `[seed, crc32("<key>=<value>")]`, or `[seed, crc32("overall")]`.
4. A slice with `n < min_n` is `low_n`. A low-n slice never blocks.
5. A slice is `below_margin` when `delta < margin` and it is not low-n.
6. A failed check blocks when its reason is in `block_on`. Otherwise its status is `warn`.
7. `policy_sha256` is the SHA-256 of the RFC 8785 canonical JSON of the policy.
8. File digests are lowercase hex SHA-256 of the file bytes, without a prefix.
9. Synthetic data: `synthetic: true` in `roadbed.yaml`, `scores.json` and `gate.json`, and a `SYNTHETIC.md` in any directory of synthetic JSONL.
