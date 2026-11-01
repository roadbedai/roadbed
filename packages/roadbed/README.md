# roadbed

Engine for Roadbed: scoring, paired bootstrap per slice, and the release gate. Prototype, v0.1 of the spec.

## Asset library (`roadbed.asset`)

Offline library over the asset tree. It does not use the network.

```python
from roadbed.asset import load, validate, row_hash, merkle_root, seal_holdout, verify

result = verify("rebases/13", objects="bucket-copy/", strict=True, trusted_keyids={"cb54bac0..."})
result.ok, result.failed  # failed is the first Check that failed, with its V-* code
```

`roadbed verify rebases/N [--objects DIR] [--strict] [--trust-key KEYID]` runs the same checks. Exit 4 names the failed check.

A rebase is full when it has a receipt, evidence or a lock (or git remembers one). A Day 0 rebase gets the 4 Day 0 checks and the same output as on Day 0.

| Code | Check (full rebase, in order) |
|---|---|
| `V-MANIFEST-*`, `V-HASH-*` | Rebase `MANIFEST.sha256`: hashes, and every file of the rebase plus the bound files (seal, holdout, snapshot manifest, recipe). |
| `V-LOCK-*`, `V-PATH-UNSAFE` | `roadbed.lock`: every file and object, sizes and hashes. No symlinks, no paths outside the asset. |
| `V-SCHEMA-INVALID` | Every file against its schema. Reader form; closed form with `--strict`. |
| `V-SYNTHETIC-FLAG` | `synthetic: true` everywhere in a synthetic asset; `SYNTHETIC.md` next to JSONL; mock receipts are synthetic. |
| `V-PII` | No email address in any file. |
| `V-SIG-*` | ed25519 signatures of all receipts and evidence manifests. `--trust-key` pins the key ids. |
| `V-MANIFEST-INCOMPLETE` | Evidence `MANIFEST.sha256` lists every evidence file and the rebase manifest. |
| `V-RECEIPT-CHAIN` | `prev_receipt` chain over all receipts, by `started_at`. |
| `V-RECEIPT-MISMATCH` | The receipt names the predictions, recipe, snapshot and holdout that are in the asset. |
| `V-POLICY-MISMATCH` | `sha256(JCS(policy/gate.yaml))` equals `gate.json` `policy_sha256`. The near check uses this policy's threshold, `num_perm` and shingle. |
| `V-SCORES-MISMATCH`, `V-SEAL-AFTER-REBASE` | The Day 0 checks. |
| `V-SEAL-INCOMPLETE`, `V-OBJECT-MISSING` | Datasets re-hashed: row hashes, Merkle roots, snapshot parts (in git or `--objects`). |
| `V-SEAL-ORDER` | Seal before training: audit seq, timestamp, git ancestry (needs a clone). |
| `V-HOLDOUT-OVERLAP` | No shared row hash, no shared input, no Jaccard >= 0.8 (raw inputs or MinHash from `--objects`). With raw holdout inputs, the sealed MinHash object must be their MinHash (else `V-HASH-MISMATCH`). |
| `V-CONTAMINATION-MISMATCH` | `contamination.json` and the gate check say what verify computed; `near.threshold` and `near.num_perm` equal the policy. |
| `V-SOD` | SoD-01 (sealer is not requester), SoD-02 (requester does not approve). |
| `V-OBJECT-MISSING` | Last check: every bucket object the asset names (lock objects, `oid:` receipt subjects) is in `--objects`. A missing one is a skip. |

Without git history or `--objects`, the git and near-overlap checks are skipped, not passed. `--strict` makes each skip a failure. The fixtures do not ship adapter weights (stand-in oids), so on them `--strict` fails only at the last check, `V-OBJECT-MISSING`.

## Watch and license policy

`roadbed.watch.scan(glob, state_path, policy)` lists the author's models on the Hugging Face Hub, keeps the ids that match the glob (author literal, wildcards in the name), pins each new base to its revision sha, and reads its license from cardData and the LICENSE file at that sha. `roadbed.license_policy.decide(card, policy)` returns allow, review or deny from `policy/license.yaml`; a base with no license is always denied. A state file reports each base once (the first scan of a glob reports only the newest match). A 24 h cache lets `--offline` and Hub outages work.

```
roadbed watch --base 'Qwen/Qwen3.*-27B'            # inside an asset repo: applies policy/license.yaml
roadbed watch --base 'Qwen/Qwen3.*-27B' --offline  # cache only, at most 24 h old
```

Only `https://huggingface.co` is contacted. No token, cookie or proxy setting is sent and redirects are not followed.

Tests replay recorded cassettes (`tests/cassettes/`) and never touch the network:

```
PYTHONPATH=packages/roadbed/tests/watch uv run pytest -q -p no_network packages/roadbed/tests/watch packages/roadbed/tests/cli/test_cli_watch.py
```

Re-record on a machine with network: delete `tests/cassettes/hub_*.yaml`, then `ROADBED_VCR_RECORD=once uv run pytest packages/roadbed/tests/watch`. Recording keeps only the `accept` request header and the `content-type`, `etag`, `x-repo-commit` response headers. `ROADBED_NETWORK=1` runs one live test.

## CLI (`roadbed`)

| Command | Where it runs | What it does |
|---|---|---|
| `roadbed init --url URL [--token-file F] [--trust-key KEYID] [--check]` | local | Writes `~/.config/roadbed/config.toml` (mode 0600). The token stays in its own file. |
| `roadbed seal --snapshot S --rows N --seed K --by KEY` | control plane | `POST /v1/holdouts` (validators only). Prints the seal. |
| `roadbed watch --base GLOB` | Hub | New bases and the license decision (W04). |
| `roadbed rebase MODEL --on REPO [--revision SHA]` | control plane | Starts a rebase and streams its timeline. Ends with `opened rebase #N → awaiting MRM sign-off` or `blocked: <slice> <delta> ...`. |
| `roadbed rebase MODEL --from-predictions rebases/N` | local | Day 0 mode: scores supplied predictions and writes the rebase files. |
| `roadbed status [--remote]` | local or control plane | Model, alias, base and recent rebases. |
| `roadbed verify rebases/N` | offline | Full asset verify (W02). |
| `roadbed eval reproduce rebases/N` | offline | Recomputes `scores.json` from the two prediction files with the stored seed. |
| `roadbed audit verify --export F [--anchor A]` | offline | Seq order, `prev_hash` links, every event hash, anchored heads. |
| `roadbed evidence verify rebases/N/evidence` | offline | Evidence manifest signature and hashes, coverage of the rebase, `approvals.json`. |
| `roadbed promote N`, `roadbed rollback --reason CODE [--to vN]` | control plane | Alias change with an `Idempotency-Key`. |

Every command takes `--json` (or `roadbed --json <command>`). Mock, replay and synthetic results are labelled in text and in JSON.

| Exit | Meaning |
|---|---|
| 0 | OK. |
| 2 | Usage: bad arguments, no asset repo, no config, a local file missing or not valid, no Hub match. |
| 3 | Remote error: the control plane or the Hub is unreachable or returned an error (401, 403 with the SoD rule and audit seq, 409, 422, 5xx). |
| 4 | A check failed and is named (`V-...`), or the gate blocked the rebase. |

Day 0 used exit 1 for input errors and 3 for a blocked gate. They are now 2 and 4.

Config (`$ROADBED_CONFIG`, else `$XDG_CONFIG_HOME/roadbed/config.toml`, else `~/.config/roadbed/config.toml`):

```toml
url = "https://cp.example"          # https; plain http only for localhost
token_file = "~/.config/roadbed/token"
trust_keys = ["<64-hex key id>"]    # verify and evidence verify accept only these keys
```

`ROADBED_URL` and `ROADBED_TOKEN_FILE` override the file. The client ignores proxy settings and does not follow redirects.

Tests: `uv run pytest -q packages/roadbed/tests/test_cli_*.py packages/roadbed/tests/cli`. Remote commands run against a respx fake of the control plane that serves the spec fixtures. The fake's bodies are checked against `tests/cli_fixtures/openapi.yaml`, a copy of `platform/contracts/openapi.yaml`.
