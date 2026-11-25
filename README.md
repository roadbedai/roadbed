# Roadbed

[![PyPI](https://img.shields.io/pypi/v/roadbed)](https://pypi.org/project/roadbed/)
[![CI](https://github.com/roadbedai/roadbed/actions/workflows/ci.yml/badge.svg)](https://github.com/roadbedai/roadbed/actions/workflows/ci.yml)
[![License](https://img.shields.io/pypi/l/roadbed)](LICENSE)
[![Python versions](https://img.shields.io/pypi/pyversions/roadbed)](https://pypi.org/project/roadbed/)

When a new open-weight base model ships, Roadbed retrains your fine-tuned model on the verdicts of your reviewers. Roadbed scores the new model per slice on a sealed holdout. A gate decides if the model is good enough. Roadbed then opens a pull request that your model-risk team signs off.

This repository is the open part. It holds the asset format, the engine, the command line tool and the capture SDK. All of it is under Apache-2.0.

## Install

You need Python 3.12 or newer.

```
pip install roadbed        # engine and command line tool
pip install roadbed-sdk    # capture SDK
```

## Quickstart: a sealed holdout in about 10 seconds

This path needs no account and no network. It takes about 10 seconds on a laptop (measured 25 November 2026).

```
pip install roadbed
roadbed sample acme-2k events
roadbed new acme --model claims-triage --synthetic --slices events/slices.yaml --output-schema events/output.schema.json
cd acme && git init -q && git add -A && git commit -qm "asset"
roadbed evals build --events ../events --as-of 2026-11-02T09:00:00Z --seed 7 --holdout-out ../vault --objects ../objects
roadbed evals seal --candidate ../vault --sealed-by vik --objects ../objects
git add -A && git commit -qm "seal"
roadbed verify data/holdout/H-2026-11-02 --objects ../objects --strict
# optional, stand-in predictions, no model runs:
python3 ../events/stand_in_predictions.py data/holdout/H-2026-11-02/holdout.jsonl rebases/1
roadbed rebase claims-triage --from-predictions rebases/1 --base Qwen/Qwen3-8B
git add -A && git commit -qm "rebase 1" && roadbed verify rebases/1 --objects ../objects
```

`vik` is the sealer's name; use your own, lowercase.

The sample holds synthetic verdicts. The `build` step turns them into a snapshot, a golden set and correction tests. It also draws a holdout.

The `seal` step writes the seal. The `verify` step checks the seal again from the files.

On a laptop, `verify` can prove these facts:

- The files are intact.
- The holdout shares no row with the training snapshot. The near-duplicate check needs readable text. With the SDK default text="tokenize" it finds nothing. Use text="redact" for text that goes to training.
- The seal commit comes before the commits with the predictions.
- The asset holds no email address.
- Every synthetic flag agrees with `roadbed.yaml`.

On a laptop, `verify` cannot prove these facts:

- When a person made the predictions. Git records the order of commits only.
- That an audit chain exists. A laptop has no audit chain.
- That roles were enforced. The person who seals is whoever types the command.
- Which model, if any, made the predictions.
- That the person who made the predictions did not see the holdout labels. They sit in the same repository, so commit order is not blindness.

## Your own verdicts

Use the SDK to capture the verdicts of your reviewers on your own machine.

```python
import os

import roadbed_sdk
from openai import OpenAI

roadbed_sdk.init_local("events", os.environ["ROADBED_TENANT_KEY"], "profile@v1", model="claims-triage", keep=("route",))
client = roadbed_sdk.wrap_openai(OpenAI())
# call the client, record verdicts with roadbed_sdk.verdict(...), then:
roadbed_sdk.flush()
```

Then run `roadbed new` and `roadbed evals build --events events` as in the quickstart. Put each string field of the model output in keep. Use short lowercase slice values. The SDK turns other strings into tokens, and the build then quarantines the row. The SDK pseudonymises free text by default. Tokens hide near-copies, so verify cannot find near-duplicates in tokenized text. Use text="redact" if the near check must work. The tenant key never leaves your process.

## What is in this repo

| Path | Content |
|---|---|
| `spec/` | The asset format `roadbed/v0.1`: rules, JSON Schemas, conformance cases, example assets. |
| `packages/roadbed/` | The engine and the `roadbed` command: scoring, paired bootstrap, gate, contamination check, offline verify. |
| `packages/roadbed-sdk/` | The capture SDK for OpenAI and Anthropic clients. |
| `examples/` | Runnable SDK snippets. |
| `docs/` | The SDK reference, the command reference and the versioning rules. |

## What the Roadbed platform adds

The platform is not in this repo. It watches for new base models and starts a rebase. It retrains the model on your GPUs. It runs the gate and signs a receipt for each run.

It opens the pull request. It keeps the audit chain and builds the evidence pack for your model-risk team. See [roadbed.ai](https://roadbed.ai).

## Status and versioning

Version 0.1.0 is the first public release. The format is `roadbed/v0.1`. It can change before 1.0. Read [docs/VERSIONING.md](docs/VERSIONING.md) for the rules.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md). Each commit needs a DCO sign-off. There is no CLA.

## Security

Report a vulnerability in private. Read [SECURITY.md](SECURITY.md).

## Licence

Apache-2.0. See [LICENSE](LICENSE).
