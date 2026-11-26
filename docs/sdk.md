# SDK reference

This file describes the public calls of `roadbed_sdk`. Only `init` and `init_local` raise errors. All other calls never raise into your app.

## Start capture

### `init_local`

```python
roadbed_sdk.init_local(events_dir, tenant_key, profile_id, *, model=None, hmac_key_id=None,
                       text="tokenize", redactor=None, keep=(), synthetic=None, spool_path=None,
                       background=True, flush_interval=1.0)
```

Writes events to JSONL files in `events_dir` with a `JsonlSink`. It needs no URL, no token and no network. Use it for the free path on a laptop.

### `init`

```python
roadbed_sdk.init(url, token, tenant_key, profile_id, *, model=None, hmac_key_id=None,
                 text="tokenize", redactor=None, keep=(), synthetic=None, spool_path=None,
                 sink=None, background=True, flush_interval=1.0, allow_http=False)
```

Sends events to a control plane. The `profile_id` is required.

| Parameter | Meaning |
|---|---|
| `tenant_key` | The HMAC key. 16 bytes or more. It stays in this process. |
| `profile_id` | The pseudonymisation profile, for example `insurer-default@v1`. |
| `text` | `tokenize` (default) or `redact`. |
| `keep` | Keys whose short label values stay as they are. |
| `synthetic` | `True` flags each interaction as synthetic. The flag is sticky. |

## Wrap a client

`wrap_openai(client)` and `wrap_anthropic(client)` patch `chat.completions.create` and `messages.create` in place. They return the same client. Streaming calls are not captured.

## Record

### `capture`

```python
with roadbed_sdk.capture(subject="claimant-123", pii=["Jane Doe"], slices={"line": "auto"}) as cap:
    client.chat.completions.create(...)
```

The context sets the subject, the known personal values, the slice labels and the interaction id for the calls inside it. Use `cap.interaction_id` to link a verdict.

### `verdict`

```python
roadbed_sdk.verdict(interaction_id, action, verdict=None, reason_code=None, *, verdict_id=None, ts=None, pii=None)
```

`action` is `approve`, `fix`, `reject` or `escalate`. A `fix` needs `verdict` and `reason_code`. The call returns the verdict id, or `None` on failure. `VerdictIn` has no reviewer id.

### `outcome` and `reconcile`

```python
roadbed_sdk.outcome(interaction_id, outcome, *, source="system_of_record", outcome_id=None, observed_at=None, pii=None)
roadbed_sdk.reconcile("outcomes.csv", id_column="interaction_id", observed_at_column="observed_at")
```

`outcome` records a late result from a system of record. `reconcile` records one outcome for each row of a CSV file and returns a report. A second run of the same file adds nothing.

### `subject_token`

`roadbed_sdk.subject_token(subject)` returns the `tok_` token of a subject id. Use it for an erasure request.

### `flush` and `shutdown`

`flush()` sends everything that is spooled now and returns the spool counts. `shutdown()` does a final flush and closes the spool. It also runs at exit.

## Sinks

A sink delivers the request bodies. The `Sink` protocol has one method: `write(kind, items) -> WriteResult`. A sink must not raise.

| Sink | Use |
|---|---|
| `JsonlSink(dir)` | Appends `interactions.jsonl`, `verdicts.jsonl` and `outcomes.jsonl`. Used by `init_local`. |
| `HttpSink(url, token)` | Sends to a control plane. Used by `init`. |

## The spool

`init` keeps a SQLite spool in WAL mode with file mode 0600. A row leaves the spool only when the server acknowledges its id. Retries wait from 0.5 s to 60 s.

## Pseudonymisation

Pseudonymisation is on by default. Free text becomes an HMAC token. Values in `pii=` and patterns such as email, phone and IBAN become placeholders. The tenant key never leaves the process. Lowercase snake_case dict keys and `keep` label values stay as raw text (rules 4 and 6). Thus a key named after a person, such as `jane_doe`, goes into the event files unless you give that name in `pii=`.

With `text="redact"`, Presidio replaces the names that it finds. Install it with `pip install 'roadbed-sdk[redact]'` and a spaCy model.

## Errors

| Error | Cause |
|---|---|
| `ConfigError` | `init` or `init_local` got a configuration that the SDK refuses, such as a short `tenant_key`. |
| `ProfileRequiredError` | `profile_id` is missing. It is a subclass of `ConfigError`. |
