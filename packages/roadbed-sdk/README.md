# roadbed-sdk

The capture SDK of Roadbed. It wraps OpenAI and Anthropic clients. It records each model call and each reviewer verdict. It pseudonymises the data in your process, before anything is stored or sent.

This is version 0.1. The format can change before 1.0.

## Install

You need Python 3.12 or newer.

```
pip install roadbed-sdk
pip install 'roadbed-sdk[redact]'   # optional: Presidio for names in free text
```

## Capture to a local folder

`init_local` writes to JSONL files in a folder. It needs no URL, no token and no network.

```python
import os

import roadbed_sdk
from openai import OpenAI

roadbed_sdk.init_local(
    "events",                          # folder for the JSONL files
    os.environ["ROADBED_TENANT_KEY"],  # 16 bytes or more. It stays in this process.
    "profile@v1",
    model="claims-triage",
    keep=("route",),                   # string fields of the model output
)
client = roadbed_sdk.wrap_openai(OpenAI())

with roadbed_sdk.capture(subject="claimant-123", slices={"line": "auto"}) as cap:
    client.chat.completions.create(model="gpt-4o", messages=messages)

roadbed_sdk.verdict(cap.interaction_id, "fix", {"route": "complex", "severity": 3},
                    reason_code="underestimated_injury")
roadbed_sdk.flush()
```

Put each string field of the model output in keep. Use short lowercase slice values. The SDK turns other strings into tokens, and the build then quarantines the row.

Then build the eval assets from the folder. Run it inside the asset that roadbed new made.

```
roadbed evals build --events ../events --as-of 2026-11-26T00:00:00Z --holdout-out ../vault --objects ../objects
```

`VerdictIn` has no reviewer id. A correction test that comes from local events gets `created_by: unattributed`.

## Send to a control plane

`init` sends the same data to a Roadbed control plane. The control plane is part of the paid platform.

```python
roadbed_sdk.init("https://roadbed.example.eu", token, tenant_key, "profile@v1", model="claims-triage")
```

`init` and `init_local` are the only calls that raise (`ConfigError`, `ProfileRequiredError`). Capture, `verdict`, `outcome`, `reconcile` and `flush` never raise into your app.

## Pseudonymisation

Pseudonymisation is on by default.

- The tenant key stays in your process. The SDK records only its key id.
- Free text becomes an HMAC token. The stored input has no readable text.
- Values that you pass in `pii=` and patterns such as email, phone and IBAN become placeholders.
- A field in `keep` and a slice label keep a short lowercase label value.
- Lowercase snake_case dict keys and `keep` label values stay as raw text (rules 4 and 6). Thus a key named after a person, such as `jane_doe`, goes into the event files unless you give that name in `pii=`.

With `text="redact"`, Presidio replaces the names that it finds. Install the `redact` extra and a spaCy model first. Tokens cannot find near-copies of a holdout input. Use `text="redact"` for text that goes to training.

## Reference

See [docs/sdk.md](https://github.com/roadbedai/roadbed/blob/main/docs/sdk.md) for each call. The source and the issue tracker are at [github.com/roadbedai/roadbed](https://github.com/roadbedai/roadbed).
