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
| `V-SCORES-MISMATCH`, `V-SEAL-AFTER-REBASE` | The Day 0 checks. |
| `V-SEAL-INCOMPLETE`, `V-OBJECT-MISSING` | Datasets re-hashed: row hashes, Merkle roots, snapshot parts (in git or `--objects`). |
| `V-SEAL-ORDER` | Seal before training: audit seq, timestamp, git ancestry (needs a clone). |
| `V-HOLDOUT-OVERLAP` | No shared row hash, no shared input, no Jaccard >= 0.8 (raw inputs or MinHash from `--objects`). |
| `V-CONTAMINATION-MISMATCH` | `contamination.json` and the gate check say what verify computed. |
| `V-SOD` | SoD-01 (sealer is not requester), SoD-02 (requester does not approve). |

Without git history or `--objects`, the git and near-overlap checks are skipped, not passed. `--strict` makes each skip a failure.
