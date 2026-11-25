# roadbed

The engine and command line tool of Roadbed. When a new open-weight base model ships, Roadbed retrains a fine-tuned model on the verdicts of your reviewers. This package scores the new model per slice, runs a paired bootstrap and applies the gate. It checks that the holdout is clean of training data. It verifies an asset offline.

This is version 0.1 of the format `roadbed/v0.1`. The format can change before 1.0.

## Install

You need Python 3.12 or newer.

```
pip install roadbed
roadbed --version
```

To capture verdicts, install the SDK: `pip install roadbed-sdk`.

## Free path on a laptop

These commands need no account and no network.

```
roadbed sample acme-2k events
roadbed new acme --model claims-triage --synthetic --slices events/slices.yaml --output-schema events/output.schema.json
roadbed evals build --events ../events --as-of 2026-11-02T09:00:00Z --seed 7 --holdout-out ../vault --objects ../objects
roadbed evals seal --candidate ../vault --sealed-by <you> --objects ../objects
roadbed verify data/holdout/H-2026-11-02 --objects ../objects --strict
```

The root README of the repository has the full quickstart.

## Commands

| Command | Where it runs | What it does |
|---|---|---|
| `roadbed new DIR --model NAME --slices F --output-schema F` | local | Writes a new asset. It has no holdout until you seal one. |
| `roadbed sample acme-2k DIR` | local | Copies the synthetic sample events to DIR. |
| `roadbed evals build --events DIR --holdout-out DIR --objects DIR` | local | Builds the snapshot, the golden set, the correction tests and a holdout candidate. |
| `roadbed evals seal --candidate DIR --sealed-by ID --objects DIR` | local | Seals a holdout candidate. The seal has `provenance: local`. |
| `roadbed verify data/holdout/H-...` | offline | Seal mode. Checks the seal, the holdout hashes and the overlap with the training data. |
| `roadbed verify rebases/N` | offline | Full mode, engine-scored mode or local mode. Local mode applies to a local seal and has no receipts. |
| `roadbed eval reproduce rebases/N` | offline | Computes `scores.json` again from the two prediction files. |
| `roadbed audit verify --export F` | offline | Checks the audit chain of an export. |
| `roadbed evidence verify rebases/N/evidence` | offline | Checks the signature and hashes of an evidence pack. |
| `roadbed rebase MODEL --from-predictions rebases/N` | local | Scores supplied predictions and writes the rebase files. |
| `roadbed watch --base GLOB` | Hub | Lists new bases and the license decision. |
| `roadbed status` | local | Shows the model, alias, base and recent rebases. |
| `roadbed init --url URL` | local | Writes the config file for a control plane. |
| `roadbed seal --snapshot S --rows N --seed K --by KEY` | control plane | Seals a holdout on the control plane. |
| `roadbed rebase MODEL --on REPO` | control plane | Starts a rebase and shows its timeline. |
| `roadbed status --remote` | control plane | Asks the control plane for the status. |
| `roadbed promote N`, `roadbed rollback --reason CODE` | control plane | Changes the alias. |

The control plane is the paid platform. It is not in this repository. Every command accepts `--json`.

A local `verify` cannot prove when the predictions were made. It reads the order of commits only. A local seal has no audit chain and no role enforcement.

| Exit code | Meaning |
|---|---|
| 0 | OK. |
| 2 | Usage error: bad arguments, a missing file or a file that is not valid. |
| 3 | Remote error: the control plane or the Hub did not answer or returned an error. |
| 4 | A check failed and has a `V-` code, or the gate blocked the rebase. |

## Library modules

- `roadbed.contracts`: the Pydantic models of the format.
- `roadbed.scoring`, `roadbed.stats`: row scores and the paired bootstrap.
- `roadbed.gate`: the release gate per slice.
- `roadbed.contamination`: exact and near-duplicate checks.
- `roadbed.asset`: seal, hash and verify an asset tree.
- `roadbed.evals`: the eval builder.
- `roadbed.labels`: the resolver that turns verdicts into labels.
- `roadbed.watch`, `roadbed.license_policy`: base model watch and license decision.

## More

- [Spec](https://github.com/roadbedai/roadbed/blob/main/spec/SPEC.md)
- [Method](https://github.com/roadbedai/roadbed/blob/main/spec/METHOD.md)
- [Source and issues](https://github.com/roadbedai/roadbed)
- [Changelog](https://github.com/roadbedai/roadbed/blob/main/CHANGELOG.md)
