# roadbed-sdk

Capture SDK for Roadbed (prototype). It wraps OpenAI and Anthropic clients, pseudonymises each interaction on the client, writes it to a local SQLite spool and sends it to the control plane in the background.

## Use

```python
import roadbed_sdk
from openai import OpenAI

roadbed_sdk.init(
    "https://roadbed.example.eu", token, tenant_key,      # tenant_key: at least 16 bytes, stays in this process
    profile_id="insurer-default@v1",                       # required: ingest without a profile is refused
    model="claims-triage", keep=("severity", "route", "line"),
    synthetic=True,                                        # synthetic tenant: every interaction is flagged
)
client = roadbed_sdk.wrap_openai(OpenAI(base_url="https://gateway.example.eu/v1"))

with roadbed_sdk.capture(subject="claimant-123", pii=["Jane Doe"], slices={"line": "auto"}) as cap:
    client.chat.completions.create(model="claims-triage-prod", messages=messages)

roadbed_sdk.verdict(cap.interaction_id, "fix", {"severity": 3, "route": "fast_track", "line": "auto"},
                    reason_code="underestimated_injury")
roadbed_sdk.outcome(cap.interaction_id, {"paid": True}, observed_at="2026-11-02T10:00:00Z")
roadbed_sdk.reconcile("outcomes.csv")   # columns: interaction_id, observed_at, then outcome fields
```

| Call | What it does |
|---|---|
| `init(url, token, tenant_key, profile_id, ...)` | Starts capture. The only call that raises (`ConfigError`, `ProfileRequiredError`). |
| `wrap_openai(client)`, `wrap_anthropic(client)` | Patch `chat.completions.create` or `messages.create` in place. Sync and async clients. |
| `capture(...)` | Context for the calls inside it: subject, known personal values, slices, interaction id. Set the id here, not in `extra_headers`. |
| `verdict(...)`, `outcome(...)`, `reconcile(csv)` | Spool `VerdictIn` and `OutcomeIn` bodies. They return the id, or `None` on failure. |
| `flush()`, `shutdown()` | Send now; final flush and close. `shutdown` also runs at exit. |
| `Sink` | Protocol: `write(kind, items) -> WriteResult`. `HttpSink` is the default. |

Bodies are `InteractionIn`, `VerdictIn` and `OutcomeIn` from `roadbed.contracts`. Interactions go to `POST /v1/interactions:batch` (500 per call). Verdicts and outcomes go to `POST /v1/verdicts` and `POST /v1/outcomes`.

## Synthetic data

A tenant that sends synthetic data passes `synthetic=True` to `init`. The flag is sticky: no capture can clear it. A `capture(synthetic=True)` flags only the calls inside it, so a wrapped call outside a capture carries no flag. The SDK logs one warning when a capture sets the flag and `init` did not.

## Personal data

- Free text becomes an HMAC token `<TEXT:tok_...>` (default `text="tokenize"`). With `text="redact"`, Presidio replaces the entities it finds with placeholders such as `<PERSON_1>`. Install it with `pip install 'roadbed-sdk[redact]'` and a spaCy model.
- Values in `pii=` and pattern entities (email, phone, IBAN, card, SSN, IP, date) always become placeholders. This includes a number equal to a `pii=` value, such as a claim number the model returns as a JSON number.
- A field in `keep` and a slice label keep a short lowercase label value (such as `fast_track`). Other values in them are tokenised.
- Numbers with 7 or more digits are tokenised, unless the field is in `keep`.
- The subject id becomes `tok_` plus 32 hex of HMAC-SHA256 with the tenant key. Only the key id (`hk_...`) is recorded.
- Values in `pii=` also match other spellings: `"123456.0"` matches the number `123456`, and `"Jane Doe"` matches the key `jane_doe`.
- NaN and infinity become `null`.
- Ids with personal data in them are not used. The SDK checks the interaction id before it sends it to the gateway as the `x-roadbed-interaction-id` header, and replaces a header that the host set. Logs hold ids and error types, never values.
- Model, alias and version strings with personal data in them become `"unknown"`. A release date in them is kept (`gpt-4o-2024-08-06`).

Limits:

- In `redact` mode, a name that Presidio does not find stays in the text.
- In `tokenize` mode, a name that the host puts in a `keep` field or a slice label stays if it is a lowercase label (for example `virtanen`). Use `keep` only for fields that hold labels.
- In `tokenize` mode, the stored input holds no text: each free-text string is one HMAC token. A near-copy of a holdout input (one character changed) then looks as far from it as an unrelated row, so the near-duplicate check (SPEC "Seal precedes training", MinHash Jaccard >= 0.8) can find exact copies only, and training cannot use the text. Use `text="redact"` for text that goes to training.

## Delivery

- The spool is SQLite in WAL mode, file mode 0600. A row leaves only when the server acknowledges its id.
- Retries back off from 0.5 s to 60 s. A 404 on a verdict or outcome is retried (its interaction may arrive later). A 422 keeps the row out of the send loop.
- One row per id: duplicate verdict and outcome ids are stored once, and the server dedupes by the same id. An interaction id is never reused: if the gateway or the host gives an id that is still in the spool, the SDK records the new interaction under a generated id and logs a warning.
- A spooled row that JSON cannot encode (NaN or infinity) is refused with reason `not_json`. The other rows still flow.
- If the server answers that an id sent for the first time is a duplicate, the SDK logs a warning: the id was reused, and the server keeps the earlier body.
- `reconcile` skips a row that fails and counts it in `skipped`. A `nan` or `inf` cell stays text.
- Streaming calls (`stream=True`) are not captured yet.

## Test

```sh
uv sync --package roadbed-sdk
uv run --package roadbed-sdk pytest packages/roadbed-sdk/tests -q            # no network
uv run --package roadbed-sdk pytest packages/roadbed-sdk/tests -q -m slow    # capture overhead, p50 under 1 ms
```
