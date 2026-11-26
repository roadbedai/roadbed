# Roadbed asset format, v0.1

Status: v0.1, amendment W01d (2026-11-25). Changes within 0.1.x are additive, except the two W01d relaxations (see Compatibility). Spec id: `roadbed/v0.1`.

An asset repo holds everything Roadbed needs to rebase one fine-tuned model onto a new base model, score it, gate it, and prove what happened.
It is plain files: a git repo of manifests, plus content-addressed objects in the customer's bucket.
One asset repo governs one model.

The JSON Schemas (Draft 2020-12) are in `schemas/v0.1/`. They are generated from the Pydantic models in `roadbed.contracts` by `export_schemas`, and a test keeps them in sync.
Two complete SYNTHETIC fixture assets are in `examples/pass/` (rebase 13, gate pass, signed off: requested by dana, validated by vik, approved by ana) and `examples/blocked/` (rebase 14, gate blocked, finding F-0001).
The names dana, vik and ana are fictional.
Their git histories are `examples/pass.bundle` and `examples/blocked.bundle` (`git clone examples/pass.bundle`), so the seal order can be checked with git.
The Day 0 example `examples/claims-triage/` stays as the minimal asset. Day 0 is the format before the W01 amendment (see "Amendments").
Valid and invalid cases for every schema, and hashing test vectors, are in `conformance/`.

A change to the format is a pull request to spec/. A maintainer approves it. See CONTRIBUTING.md.

## Glossary

| Term | Meaning |
|---|---|
| Base | An open-weight model from the Hugging Face Hub, pinned by repo and revision sha. |
| Version | One trained adapter on one base, `vN` (`versions/vN/`). The alias serves one version. |
| Rebase | Retraining the model on a new base, then scoring and gating it. Numbered per tenant, so an asset repo can have gaps (`rebases/13`, `rebases/15`). |
| Incumbent | The version that serves today. Candidate: the version the rebase trained. |
| Verdict | A reviewer's label for one interaction (approve, fix, reject, escalate). |
| Snapshot | A frozen set of resolved verdict rows that training reads (`S-<date>`). |
| Holdout | Rows kept out of training and sealed by a validator (`H-...`). Scores come only from it. |
| Seal | `SEAL.json`: who sealed the holdout, when (time and audit seq), and its hashes. A local seal (`provenance: local`, made by `roadbed evals seal` on a laptop) names a person and has no audit seq. |
| Slice | One value of one slice key, e.g. `line=bodily_injury`. The gate checks every slice. |
| Gate | The policy check that passes or blocks a rebase (`gate.json`). |
| Receipt | A signed record of one run: inputs, environment, cost, executor. |
| Evidence pack | The signed files a model-risk team reads before sign-off (`rebases/N/evidence/`). |
| SoD-01 | Separation of duties: the user who sealed the holdout cannot request the run. |
| SoD-02 | The user who requested the rebase cannot approve or promote it. |
| SoD-03 | The validator and the approver of a rebase are different users. |
| SoD-04 | An admin never approves or validates a rebase. The control plane enforces it. A local asset has no roles. |
| SoD-05 | Only a validator seals a holdout or reads holdout rows. The runner service reads rows for eval jobs only. |

## The asset tree

```
<asset>/
├── roadbed.yaml                        root manifest
├── roadbed.lock                        every tracked path -> sha256 + size; every bucket oid; sorted, deterministic
├── policy/gate.yaml                    margin, min_n, ci, n_boot, seed, block rules, stages, cost cap
├── policy/license.yaml                 allow, review, default deny
├── policy/keys/*.pub                   ed25519 public keys (PEM)
├── data/snapshots/<date>/MANIFEST.json rows, schema hash, Merkle root, part oids, label rules, pseudonymisation profile + HMAC key id
├── data/holdout/<id>/SEAL.json         sealed_by (validator), sealed_at, audit_seq, seal commit, seed, stratification, Merkle root, MinHash oid
├── data/holdout/<id>/holdout.jsonl     sealed holdout rows
├── data/tombstones.jsonl               erasure requests
├── evals/{golden,correction_tests}.jsonl, slices.yaml, scorers.yaml, output.schema.json
├── recipes/<family>/<name>@<version>.yaml
├── versions/vN/{model.json, receipt.json, receipt.sig}
├── rebases/N/{rebase.yaml, receipt.json, receipt.sig, predictions.incumbent.jsonl, predictions.candidate.jsonl,
│              scores.json, gate.json, contamination.json, cost.json, license.json, bom.cdx.json, report.md, MANIFEST.sha256}
├── rebases/N/evidence/{approvals.json, MANIFEST.sha256, MANIFEST.sig[, evidence.md, evidence.html, evidence.pdf]}
├── findings/F-NNNN.yaml
├── lineage/edges.jsonl                 append-only {from, to, kind, hashes, ts, actor}
└── audit/anchors/<date>.json           daily audit-chain head (also in the Object Lock bucket)
```

## Files and schemas

| Path (from asset root) | Schema | Purpose |
|---|---|---|
| `roadbed.yaml` | `roadbed` | Root manifest: model, tenant, task and output schema, base repo and revision, data (holdout once sealed, verdicts, snapshot), evals (scorers, slices, margin, min_n, golden, correction tests), recipe, gate policy, alias, license policy, lineage. |
| `roadbed.lock` | `lock` | Every tracked file except itself (path, sha256, size), sorted bytewise by path. Every bucket object the tree names (oid, size, the path that names it), sorted by oid. JSON. |
| `<path>.ptr` | `object-pointer` | Stands in git for a file over 1 MB. The bytes live in the bucket at `objects/sha256/<oid[0:2]>/<oid[2:4]>/<oid>`. |
| `policy/gate.yaml` | `gate-policy` | margin, min_n, ci, n_boot, seed, block_on, stages (stub), optional cost_cap and contamination settings. |
| `policy/license.yaml` | `license-policy` | allow (lowercase SPDX ids), optional review (ids or globs) and default (`deny` when absent). |
| `policy/keys/<name>.pub` | (PEM) | ed25519 public key, SubjectPublicKeyInfo PEM. `receipt.pub` verifies receipts; `evidence.pub` verifies evidence manifests. |
| `data/snapshots/<date>/MANIFEST.json` | `snapshot-manifest` | One frozen training snapshot: id `S-<date>`, rows, schema hash, Merkle root, parts, label rules, pseudonymisation profile and HMAC key id, parent. |
| snapshot part (`part-NNNN.jsonl` or a bucket object) | `snapshot-row` | One resolved verdict row per line, with its row hash. Training reads these. |
| `data/holdout/<id>/SEAL.json` | `seal` | The seal. Written once, by a validator on the control plane, or by a named person for a local seal (`provenance: local`, no `audit_seq`, always full, see "Local seal"). `merkle_root`, `minhash_oid`, `snapshot` and `stratification` come together: all present (a full seal) or all absent (a Day 0 seal, which supports no disjointness check). |
| `data/holdout/<id>/holdout.jsonl` | `holdout-row` | Sealed holdout: id, slice values, input content address, reviewer verdict, row hash. No raw input. |
| `data/tombstones.jsonl` | `tombstone-row` | Erasure requests, by HMAC subject token. |
| `evals/golden.jsonl` | `golden-row` | Golden set rows. |
| `evals/correction_tests.jsonl` | `correction-test-row` | One test per reviewer fix: model output, expected fix, reason code. |
| `evals/output.schema.json` | (tenant-defined) | JSON Schema of one model output and one reviewer verdict. |
| `evals/scorers.yaml` | `scorers` | Weighted exact match of output fields, 0 to 100. |
| `evals/slices.yaml` | `slices` | Slice keys and their values. Each value of each key is one slice. |
| `recipes/<family>/<name>@<version>.yaml` | `recipe` | Trainer, LoRA r, alpha, targets, lr, epochs, seed, chat template, image digest. |
| `versions/vN/model.json` | `model-version` | One trained version: base, license, adapter oid, recipe, snapshot, holdout, receipt path. |
| `versions/vN/receipt.json`, `rebases/N/receipt.json` | `receipt` | Signed run record. in-toto Statement v1. |
| `versions/vN/receipt.sig`, `rebases/N/receipt.sig`, `rebases/N/evidence/MANIFEST.sig` | `signature` | ed25519 signature of one file. |
| `rebases/N/rebase.yaml` | `rebase` | Rebase metadata: base, from and to versions, created_at, trigger, materiality, mode, orchestrator state, requester, PR. |
| `rebases/N/predictions.{incumbent,candidate}.jsonl` | `prediction-row` | Outputs of the production model and the candidate on the holdout. |
| `rebases/N/scores.json` | `scores` | Overall and per-slice scores, deltas, bootstrap CIs, input file hashes. |
| `rebases/N/gate.json` | `gate` | pass or blocked, the checks, findings. |
| `rebases/N/contamination.json` | `contamination` | Exact and near overlap between train and holdout, and the seal order. |
| `rebases/N/cost.json` | `cost` | GPU, price, train and predict GPU-hours, bench latency and cost per 1k calls, vLLM version and flags. |
| `rebases/N/license.json` | `license-decision` | License read from the base, its source, and the policy decision. |
| `rebases/N/bom.cdx.json` | (CycloneDX 1.6) | ML-BOM: base model, adapter, snapshot, holdout. |
| `rebases/N/report.md` | (text) | Pull request body made from scores.json and gate.json. |
| `rebases/N/roadbed.yaml.proposed` | `roadbed` | Optional (Day 0). The base bump the PR applies after sign-off. |
| `rebases/N/MANIFEST.sha256`, `rebases/N/evidence/MANIFEST.sha256` | `evidence-manifest` (parsed) | `sha256sum` style list of files. See "Manifests". |
| `rebases/N/evidence/approvals.json` | `approvals` | Sign-off status ("awaiting MRM sign-off"), required roles, approvals, pack version. |
| `rebases/N/evidence/evidence.{md,html,pdf}` | (text, HTML, PDF) | Rendered evidence pack. Made by the platform. Optional: see "Manifests". |
| `findings/F-NNNN.yaml` | `finding` | Logged findings. Ids are F-0001, F-0002, ... in order. |
| `lineage/edges.jsonl` | `lineage-edge` | Append-only lineage graph. |
| `audit/anchors/<date>.json` | `audit-anchor` | Daily head of the audit hash chain. |

Schemas for API bodies and runner messages (not asset files) are in the same directory: `audit-event` (also one line of the audit export), `approval`, `executor-job`, `job-lease`, `lease-state`, `heartbeat`, `job-status`, `job-result`, `alias-config`, `interaction-in`, `verdict-in`, `verdict-record`, `outcome-in`.
`packages/roadbed/tests/cli_fixtures/openapi.yaml` copies them, pinned to this spec version.

- **Ingest ids.** `interaction_id`, `verdict_id` and `outcome_id` are client-made, 1 to 128 characters.
- **Verdict actor.** A stored verdict (`verdict-record`) is the `VerdictIn` body plus `actor`, a user id. The server sets `actor` from the authenticated principal (session user or API token owner). The request body has no `actor`; a body that sends one gets 422.

## Compatibility within v0.x

- **Additive only.** Within v0.x, producers only add optional fields. They never remove, rename or retype a field, and never narrow an allowed value.
- **Producers are strict.** Whoever writes a file, a response or a request validates it against the published schema in `schemas/v0.1/`. The published schemas are closed (`additionalProperties: false`). A field enters the contract only through a spec change.
- **Readers are lenient.** Consumers of files and responses ignore unknown fields: the Pydantic models of files and responses use `extra="ignore"`, and `json_schema(name, reader=True)` is the schema without `additionalProperties: false`. `roadbed verify` and `load_asset` read this way. So an older CLI, runner or gateway reads a newer producer's output.
- **Requests are strict.** Request bodies (`InteractionIn`, `VerdictIn`, `OutcomeIn`, `JobStatus`, `JobResult`, `Heartbeat`) use `extra="forbid"`. The server rejects an unknown field with 422.
- **Markers.** A file that has a `spec` field keeps `spec: roadbed/v0.1` for every additive change. Row files (JSONL) and policy files carry no marker; the asset's `roadbed.yaml` speaks for them.
- **W01d relaxations.** W01d relaxes two required fields: `SEAL.json` `audit_seq` is required unless `provenance` is `local`, and `roadbed.yaml` `data.holdout` is optional until a holdout is sealed. Every v0.1 file stays valid. A reader older than W01d refuses a local seal or an unsealed `roadbed.yaml` with a schema error. See [CHANGELOG.md](../CHANGELOG.md).
- **v0.2.** A change that is not additive is v0.2. It is signalled by `spec: roadbed/v0.2` in `roadbed.yaml` and in each file that has a marker, and by a new schema directory `schemas/v0.2/`. A v0.2 reader accepts both markers (its models type `spec` as `roadbed/v0.1 | roadbed/v0.2`) and reads v0.1 assets as they are. A v0.1 reader refuses a v0.2 marker with a clear error. Breaking changes after v0.x need a migration command.

## Core rules

These rules are fixed for v0.1. The sections below make them exact.

> Rules: sha256 over raw bytes for files, `sha256(JCS(obj))` for JSON, row hash over `{interaction_id, input_ref, output, verdict, reason_code, outcome, ts}`, dataset identity is the Merkle root of sorted row hashes. Files over 1 MB live in the bucket as `objects/sha256/ab/cd/<oid>` with a pointer in git. A rebase is valid only if the seal precedes training three ways (audit seq, timestamp, git ancestry) and train and holdout share no row hash and no MinHash Jaccard ≥ 0.8. Audit, lineage, receipts and evidence hold ids, HMAC tokens, hashes and aggregates, never personal data. Deletion is a tombstone plus a new snapshot plus an `erased_from` edge. `roadbed verify <rebase>` re-hashes, re-validates, checks signatures, seal order and disjointness, and recomputes `scores.json` from the two predictions files with the stored seed; exit 4 names the failed check.

## Hashing

0. **Hash what is stored.** A file digest is over the stored bytes. A JSON digest is `sha256(JCS(obj))` of the stored JSON as parsed, never of a model re-dump. Models keep every stored value as it was (timestamps stay strings), and a test proves a model round trip changes no hash in the fixtures. Store what you hash; hash what you stored.
1. **File digest.** Lowercase hex SHA-256 of the file bytes, without a prefix.
2. **JSON object digest.** `sha256(JCS(obj))`, where JCS is RFC 8785 canonical JSON. Use it when a JSON value is identified independent of its formatting: receipts in a chain, policies (`policy_sha256`, null fields left out), output schemas (`schema_sha256`), audit events.
3. **Content address.** `input_ref` is `sha256:<hex>` of JCS(pseudonymised input); also its bucket oid. The input is a JSON object (for example `{"narrative": ..., "line": ...}`), pseudonymised by the SDK before it is hashed. The raw input is not stored in the asset.
4. **Row hash.** `sha256(JCS({interaction_id, input_ref, output, verdict, reason_code, outcome, ts}))`. All seven keys are present; a missing value is `null`. `ts` is the string as stored. Snapshot rows carry it as `row_hash`. Holdout, golden and correction-test rows carry the row hash of the verdict row they came from. A Day 0 holdout row without `row_hash` uses `sha256(JCS(row))`. The optional `synthetic` flag of a row is part of the stored row: hash the stored row as is. So it is in `sha256(JCS(row))` when present, and it is never one of the seven row-hash keys.
5. **Dataset identity.** The Merkle root of the row hashes sorted ascending (hex order, duplicates kept), built as RFC 6962 section 2.1: leaf = `sha256(0x00 || 32 digest bytes)`, node = `sha256(0x01 || left || right)`, split at the largest power of two below n. The empty set hashes to `sha256("")`. Vectors: `conformance/vectors/hashing.json`.
6. **Bucket objects.** An object id (oid) is the file digest of the object bytes. The key is `objects/sha256/<oid[0:2]>/<oid[2:4]>/<oid>`. A tracked file over 1 MB is replaced in git by `<path>.ptr` (schema `object-pointer`). `roadbed.lock` lists every oid the tree names.
7. **Lock.** `roadbed.lock` lists every tracked file except itself. Paths are `/` separated, from the asset root, sorted by their UTF-8 bytes. Rewrite the lock last, after every other file.
8. **Timestamps.** Every timestamp is UTC, RFC 3339, seconds precision, with a `Z` suffix: `YYYY-MM-DDTHH:MM:SSZ` (type `UtcTs`). No fraction, no offset. One form means one hash, and the strings sort in time order. `2026-11-01T09:02:00.120Z` and `2026-11-01T09:02:00+00:00` are invalid. Order events by audit seq, not by sub-second time.

| Digest form | Where |
|---|---|
| bare lowercase hex (64) | file digests, oids, `row_hash`, Merkle roots, `*_sha256` fields, MANIFEST lines, `keyid` |
| `sha256:<hex>` | `input_ref` (content address) and OCI image digests (`image_digest`) |
| `{"sha256": "<hex>"}` | in-toto `subject[].digest` (the in-toto layout) |

## Manifests

A `MANIFEST.sha256` is text: one line per file, `<sha256>  <path>` (two spaces), paths from the asset root, sorted, newline-terminated.

- `rebases/N/MANIFEST.sha256` lists every file under `rebases/N/` except itself and the `evidence/` directory, plus the holdout file, plus the bound files: the holdout `SEAL.json`, the training snapshot `MANIFEST.json` (`<data.verdicts>/<date>/MANIFEST.json` for snapshot `S-<date>`) and the recipe file. So the signed evidence manifest covers the seal.
- **Day 0 rule.** A rebase without `receipt.json` (spec v0.1, Day 0) needs only the files its manifest lists. A rebase with `receipt.json` must list every bound file that exists, or verify fails with `V-MANIFEST-INCOMPLETE`.
- `rebases/N/evidence/MANIFEST.sha256` lists every file under `evidence/` except the manifest and its signature, plus `rebases/N/MANIFEST.sha256`. So it covers the whole rebase. Each new approval writes a new `approvals.json`, a new evidence manifest and a new signature (a new pack version). The rebase manifest does not change.
- **Approvals-only evidence.** The rendered pack (`evidence.md`, `evidence.html`, `evidence.pdf`) is optional for a Day 0 asset. An `evidence/` directory that holds only `approvals.json`, `MANIFEST.sha256` and `MANIFEST.sig` is valid when its manifest lists no rendered file. The fixtures in `spec/examples` carry this form: no renderer ran. `roadbed verify` then says "approvals only, no rendered evidence pack", and an evidence verifier checks the approvals, the manifest and the signature and does not ask for the rendered files. When the manifest lists one rendered file, the full pack rules apply.

## Signatures and receipts

- **Keys.** ed25519. A key file is SubjectPublicKeyInfo PEM under `policy/keys/`. The key id is the lowercase hex SHA-256 of the raw 32-byte public key.
- **Signature file** (`*.sig`, schema `signature`). JSON: `alg` (`ed25519`), `keyid`, `key` (the key path), `signed` (the signed file path), `payload`, `sig` (base64, padded). `payload: jcs` signs the JCS bytes of the signed JSON file; receipts use it. `payload: raw` signs the file bytes; manifests use it.
- **Receipt** (`receipt.json`). An in-toto Statement v1: `_type` `https://in-toto.io/Statement/v1`, `subject` (repo paths, or `oid:<hex>` for bucket objects, with sha256 digests), `predicateType` `https://roadbed.dev/run/v1`, and `predicate`.
- **Predicate fields.** job (id, kind), model, rebase, base (repo and revision sha), recipe (path and sha256), snapshot (id and Merkle root), holdout (id, Merkle root, seal seq, sealed_at, seal commit), image digest, uv.lock sha256, gpu, seed, started_at, ended_at, audit_seq (job start), prev_receipt, cost, executor, config (optional).
- **Config.** `predicate.config` binds the evaluation configuration: `roadbed_yaml_sha256`, `slices_sha256`, `scorers_sha256`, `gate_policy_sha256`, `seal_sha256` and `lock_sha256`. Each is the sha256 of the file bytes the job ran against: `roadbed.yaml`, the `evals.slices` and `evals.scorers` files, the gate policy file (`policy/gate.yaml`; this is not the JCS `policy_sha256` of `gate.json`), the holdout `SEAL.json`, and the `roadbed.lock` the job started from. That lock never lists the receipt itself, so it is the lock in the commit that added the receipt, not a later one. Each field is optional for compatibility. The control plane always fills all six for a rebase receipt. `roadbed verify` checks each field that is present: against the file in the commit that added the receipt when git history is there, otherwise against the working tree. The five config files must also match in the working tree, because verify and the gate read them there. Without git history, a working tree lock that lists the receipt is not the start lock, so the `lock_sha256` check is skipped (a failure with `--strict`). A mismatch is `V-HASH-MISMATCH`.
- **Executor.** `executor` is required. Its `name` is one of `runner`, `mock`, `replay`, `hf-jobs`, `together`. Mock and replay output always says so. A demo number never comes from a receipt whose executor is `mock`.
- **No GPU for mock.** When the executor is `mock` or `replay`, `gpu.name` is `none`, `gpu.count` is 0 and every cost number is 0, in the receipt and in `cost.json`.
- **Snapshot and holdout.** A receipt of job kind `train`, `eval` or `rebase` names `snapshot` and `holdout`; schema and model reject one without them. A `bench` receipt may leave both out.
- **Receipt path.** The runner signs the receipt with `signed` set to the job's `receipt_path` (`versions/vN/receipt.json` or `rebases/N/receipt.json`). The control plane stores the receipt at that path, so the signature verifies in the asset as it is.
- **Job results.** A `JobResult` with state `succeeded` carries both `receipt` and `receipt_sig`.
- **Chain.** `prev_receipt` is `sha256(JCS(previous receipt.json))` of the previous receipt in this asset, by `started_at`. The first receipt has none (absent or null).

## Seal precedes training

A rebase is valid only if all of these hold:

1. **Audit seq.** `SEAL.json.audit_seq` < the training receipt's `predicate.audit_seq`. A local seal has no audit seq, so no receipt can bind it (see "Local seal").
2. **Timestamp.** `SEAL.json.sealed_at` < `predicate.started_at`.
3. **Git ancestry.** The seal commit is an ancestor of the train commit, and they differ. The seal commit is the commit that added `SEAL.json`: `git log --diff-filter=A --format=%H -- data/holdout/<id>/SEAL.json`. The train commit is the commit that added the training `receipt.json`, found the same way. Both come from git, never from a stored field. `SEAL.json.seal_commit` is informational only; a file cannot name the commit that adds it.
4. **No exact overlap.** The training snapshot and the holdout share no row hash.
5. **No near overlap.** No pair of a training row and a holdout row has MinHash Jaccard ≥ 0.8 (`policy/gate.yaml` `contamination.minhash_threshold`; the holdout signatures are the object `SEAL.json.minhash_oid`). Small sets may use exact Jaccard (`near.method: jaccard`) over the same shingles.

**MinHash input, exactly.** Every producer and checker (seal, runner, contamination, verify, dataset builders) computes it this way:

- **Text.** The value of `input.narrative`, or of the single free-text input field that `roadbed.yaml` `task.text_field` names. The input is the pseudonymised JSON object at oid `input_ref` (Hashing, rule 3). Other input fields are not part of the text.
- **Tokens.** Lowercase the text (Unicode lowercase), then take every match of `[a-z0-9<>_]+` in order. Placeholders such as `<person_1>` stay one token.
- **Shingles.** Word k-shingles with k = `contamination.shingle` (3): each run of k consecutive tokens, joined by one space, as a set. A text with k or fewer tokens is one shingle (all its tokens). A text with no token has no shingles and is left out of the near check (the exact check still covers it).
- **Signature.** `contamination.num_perm` (128) slots. Slot j, for j = 0 to num_perm − 1, is the minimum over the shingles s of the first 8 bytes of `sha256(UTF-8 "<j>|<s>")`, written as 16 lowercase hex. The slot index j is the only key: there is no random seed or salt, so every party gets the same signature.
- **Estimate.** The MinHash Jaccard of two rows is the share of slots with equal values. A pair is near when it is ≥ the threshold.
- **Holdout object.** `SEAL.json.minhash_oid` names the bytes `JCS({"num_perm", "shingle", "signatures": {<row_hash>: [<16 hex>, ...]}})`.

The receipt binds the seal: `predicate.holdout` repeats `seal_seq`, `sealed_at`, `merkle_root` and the seal commit, and verify compares them with `SEAL.json` and git.

`contamination.json` reports the result. Its `seal_order` holds the two commits and one boolean per leg (`audit_seq`, `timestamp`, `git_ancestor`), plus `ok`. It copies no seqs or times: verify reads those from `SEAL.json` and the receipt and recomputes every leg. `ok` is true only when all three legs are true. `status: pass` needs `exact.shared` 0, no near pairs and `seal_order.ok`. The gate's `contamination` check has the same status as `contamination.json`.

Only a validator seals or reads holdout rows (SoD-05), and the sealer cannot request the run (SoD-01). A local seal names a person. No role is enforced for it.

## Local seal

A holdout sealed on a laptop by `roadbed evals seal` carries `provenance: local` and no `audit_seq`.
The sealer is the person named in `sealed_by`. No role is enforced, so SoD-01 and SoD-05 are not checkable.
The seal is full: `stratification`, `merkle_root`, `minhash_oid` and `snapshot` are present, so a Day 0 local seal is not valid.
The MinHash object lives in the local object store (`--objects`). The raw holdout inputs stay with the sealer, outside the asset.

The anchor for the order is git: the seal commit must be a strict ancestor of the commits that added the rebase's prediction files.
This proves commit order, not when the predictions were made. `sealed_at` and `rebase.yaml` `created_at` are self-declared.
A control-plane receipt cannot bind a local seal (`predicate.holdout.seal_seq` is required), so a run over a local seal cannot be attested.

`roadbed verify` names each check that it cannot run on a local asset as a skip. `--strict` fails on the first skip.
Record the Merkle root and the seal commit outside the repository.

## Lineage

- `lineage/edges.jsonl` is append-only. Never rewrite a line.
- An edge reads "`from` `kind` `to`": `rebase:13 trained_on snapshot:S-2026-11-01`.
- Node ids are `<type>:<id>`. Types: `snapshot`, `holdout`, `version`, `rebase`, `subject`, `recipe`, `base`.
- Kinds: `sealed_from`, `trained_on`, `evaluated_on`, `rebased_from`, `produced`, `promoted_to`, `erased_from`.
- `hashes` holds the dataset roots or file digests of the nodes, keyed `from` and `to`.
- **Deletion.** Deletion is a tombstone (`data/tombstones.jsonl`), plus a new snapshot without the subject's rows (`parent` names the old one), plus an edge `snapshot:<new> erased_from snapshot:<old>`.

## Personal data

Audit events, lineage, receipts and evidence hold ids, HMAC tokens (`tok_<hex>`), hashes and aggregates. They never hold personal data. The schemas enforce it:

- **Actor ids.** Every actor, sealer, requester and approver field is a user or service id: `^[a-z0-9][a-z0-9_-]{0,63}$`. No email address, no name.
- **Subjects.** A lineage subject node is `subject:tok_<hex>`. Other nodes are `<type>:<id>`.
- **Audit payload.** Keys match `^[a-z_]{1,64}$`. Values are numbers, booleans, null, or strings matching `^[A-Za-z0-9:_./+=-]{1,256}$` (no spaces). Free-text keys (`note`, `comment`, `text`, `message`, `description`, `detail`, `reason`, `name`, `email`, `phone`) are rejected.
- **Approvals.** An approval has a `reason_code` (enum) and `refs` (finding, correction-test, audit or rebase ids). It has no free-text comment.
- **Short text fields.** `note` (findings, rebase.yaml), `message` (job status) and `error` (job result) can reach evidence. They hold ids and plain facts only, never personal data.

The SDK pseudonymises before ingest. Ingest without a pseudonymisation profile id is rejected (422).

## Audit chain

Each event has `seq` (from 1), `prev_hash` (the `hash` of seq − 1, 64 zeros for seq 1) and `hash = sha256(JCS(event without "hash"))`.
A denied event names its `rule`: `SoD-01` to `SoD-05`, or `RBAC` for a deny by the role matrix.
`object.type` matches `^[a-z][a-z_]{0,63}$`. `object.id` is an id, token or hash, or a lone `*` for a collection (list routes): `^(\*|[A-Za-z0-9:_./+=-]{1,256})$`. It has no `@`, so it never holds an email address. Payload integers and numbers stay within ±9007199254740991 (2^53 − 1), so every JSON reader holds them exactly.
A daily anchor (`audit/anchors/<date>.json`) records the head seq and hash. A copy goes to the Object Lock bucket. Vectors: `conformance/vectors/audit-chain.jsonl`.

## Scoring and gate

1. Row score = 100 x sum(weight x exact match) / sum(weight). A missing field scores 0.
2. Delta = candidate mean − incumbent mean, in score points, on the same rows (paired).
3. CI = percentile bootstrap of the mean paired difference, `n_boot` resamples, level `ci`. The RNG seed is `[seed, crc32("<key>=<value>")]`, or `[seed, crc32("overall")]`.
4. A slice with `n < min_n` is `low_n`. A low-n slice never blocks.
5. A slice is `below_margin` when `delta < margin` and it is not low-n.
6. **Gate names.** Each check has a fixed name, and each name has one failure reason. A finding's `kind` is that reason. `block_on` lists reasons.

| Check `name` | Failure reason (`block_on`, finding `kind`) |
|---|---|
| `slice_margin` | `slice_below_margin` |
| `license` | `license_denied` |
| `contamination` | `contamination` |
| `seal_order` | `seal_order` |
| `stage:shadow`, `stage:canary`, `stage:bias_tests` | (stubs: always `skipped`) |

7. A failed check blocks when its reason is in `block_on`. Otherwise its status is `warn`. A gate with status `pass` has no failed check. Every check has a non-empty `detail`; a skipped check says why.
8. `policy_sha256` is the SHA-256 of the RFC 8785 canonical JSON of the policy, null fields left out.
9. Synthetic data: `synthetic: true` in `roadbed.yaml`, `scores.json`, `gate.json` and every other file or row that has the field, a `SYNTHETIC.md` at the asset root, and, for each synthetic JSONL file, a `SYNTHETIC.md` in its directory or in any parent directory below the asset root.

## Verify

`roadbed verify rebases/N` checks integrity, not the gate outcome. A blocked rebase still verifies. Exit 0 means every check passed. Exit 4 names the first failed check (`V-...`).

A Day 0 rebase (no receipt, no evidence, no lock) gets the Day 0 checks, in order: the rebase `MANIFEST.sha256` hashes, the `scores.json` and `gate.json` schemas (reader form), `scores.json` recomputed from the two prediction files with the stored seed, and the seal time before the rebase time.

For each receipt in the asset, the transcript names the executor and the key id of its signature, for example `receipt rebases/13/receipt.json: executor mock, signed by cb54bac00a07`. When a receipt is `mock` or `replay`, the last line is `MOCK: no model was trained`. `--json` carries the same list as `executors` and the flag as `mock`.

A full rebase is **engine-scored** when `roadbed rebase --from-predictions` scored it from runner jobs: signed runner receipts in `rebases/N/receipts/` and no control-plane `receipt.json`, `receipt.sig` or `evidence/` (on disk, in the manifest or in git history). It gets these checks: the hashes, the files of the lock, the runner receipt signatures, and the receipts against the predictions and the holdout.
It also gets the policy hash, the scores and the gate recomputed, and the seal order by audit seq and time.
Seven checks are named skips, because such a run does not produce their inputs.
These are the full lock, the receipt chain, the git ancestry, `contamination.json`, `license.json`, `bom.cdx.json` and `evidence/`.

`roadbed verify data/holdout/<id>` checks one sealed holdout:

- The seal schema, and the holdout re-hashed (sha256, rows, Merkle root).
- The holdout rows against the closed `holdout-row` schema, so no raw input field.
- Personal data in the holdout, and the synthetic flags of every file and row in the asset (rule 9).
- The seal committed and unchanged (git).
- The MinHash object: it covers exactly the sealed rows, with the policy's `num_perm` and `shingle`.
- The eval sets: no row that was ever in `evals/golden.jsonl` or `evals/correction_tests.jsonl` shares an id, input or row hash with the holdout.
- Exact and near disjointness from the snapshot that the seal names.

A control-plane seal gets the same checks plus a named skip for its audit seq. A signed receipt binds the audit seq at rebase time, and only `roadbed verify rebases/N` checks it.
With a git clone and `--objects`, a local seal passes `--strict` with no skip:

```
$ roadbed verify data/holdout/H-2026-11-02 --objects ../objects --strict
seal schema OK  (closed form)
holdout re-hashed  (296 rows, sha256 and Merkle root match SEAL.json)
holdout rows OK  (holdout-row schema, closed form: no raw input field)
no personal data (email) in the holdout  (SEAL.json and holdout.jsonl scanned)
synthetic flags OK  (2173 flags, SYNTHETIC)
seal committed and unchanged  (commit dae9f7e356a8)
MinHash object covers the sealed rows  (1bed66c23fd2, num_perm 128, shingle 3)
eval sets disjoint from the holdout  (281 rows ever in golden or correction tests: 0 shared ids, inputs, row hashes)
datasets re-hashed  (S-2026-11-02 1579 rows, H-2026-11-02 296 rows, Merkle roots match)
train and holdout disjoint: exact  (0 shared row hashes, 0 shared inputs (1579 x 296))
train and holdout disjoint: near (Jaccard < 0.8)  (minhash, max 0.507812)
verified seal H-2026-11-02
LOCAL seal: sealed by vik; no audit chain; no role enforcement
```

A full rebase is **local** when the asset's holdout has a local seal and the asset has no control-plane file. That is: no `roadbed.lock`, `policy/keys/`, version or rebase receipt, receipt signature, `evidence/` or `receipts/`, on disk, in the manifest or in git history. Its predictions are supplied, not attested.

A local rebase gets the seal checks, and the scores and the gate recomputed.
It also gets the personal-data scan and the synthetic flags of the asset, and the seal time not after `rebase.yaml` `created_at`.
The git ancestry of the seal before both prediction files is a check too.
Last, it gets exact and near disjointness from the training snapshot (`roadbed.yaml` `data.snapshot`).
Each check that only the control plane can supply is a named skip.

A local rebase never passes `--strict`: it fails at "receipts signed", and the skips are the list of what the control plane adds.

A local seal can pass `--strict`, so its exit code is 0, the same as for a control-plane seal.
A script must read the `LOCAL` line of the text output, or `local: true` in the JSON output.
The exit code alone does not tell a local seal from a control-plane seal.

```
$ roadbed verify rebases/1 --objects ../objects
hashes OK  (10 files)
no personal data (email) in the asset  (24 files scanned)
seal schema OK  (reader form)
holdout re-hashed  (296 rows, sha256 and Merkle root match SEAL.json)
holdout rows OK  (holdout-row schema, closed form: no raw input field)
no personal data (email) in the holdout  (SEAL.json and holdout.jsonl scanned)
synthetic flags OK  (2177 flags, SYNTHETIC)
seal committed and unchanged  (commit dae9f7e356a8)
MinHash object covers the sealed rows  (1bed66c23fd2, num_perm 128, shingle 3)
eval sets disjoint from the holdout  (281 rows ever in golden or correction tests: 0 shared ids, inputs, row hashes)
receipts signed: skipped  (no receipt: predictions supplied, no run attested)
receipt chain OK: skipped  (no receipts in a local asset)
receipts bind the predictions and the holdout: skipped  (no receipt; the unsigned rebase manifest names them)
lock files re-hashed: skipped  (no roadbed.lock in a local asset; git history is the anchor)
gate policy matches gate.json  (95c7e2f84804 (policy/gate.yaml))
schemas OK  (scores.json, gate.json)
predictions cover the sealed holdout  (H-2026-11-02 296 rows, Merkle root matches; both prediction files)
scores recomputed: match  (overall + 14 slices, 6 decimals)
gate recomputed: match  (status pass, 6 checks, 0 findings)
seal not after rebase  (sealed_at not after rebase.yaml created_at; both are self-declared)
seal precedes training: audit seq: skipped  (no audit chain in a local asset)
seal precedes predictions: git ancestry  (dae9f7e356a8 < 5ee026e5d49d; commit order only)
datasets re-hashed  (S-2026-11-02 1579 rows, H-2026-11-02 296 rows, Merkle roots match)
train and holdout disjoint: exact  (0 shared row hashes, 0 shared inputs (1579 x 296))
train and holdout disjoint: near (Jaccard < 0.8)  (minhash, max 0.507812)
contamination.json matches: skipped  (not produced by a local rebase)
license.json: skipped  (not produced by a local rebase; the gate license check is recomputed)
bom.cdx.json: skipped  (not produced by a local rebase)
evidence manifest OK: skipped  (no evidence/ in a local rebase)
segregation of duties (SoD-01, SoD-02): skipped  (local asset: no role enforcement; the sealer may run the rebase)
bucket objects re-hashed  (1 objects)
verified rebase #1
LOCAL: no run was attested; sealed by vik; no audit chain; no role enforcement
```

A full or engine-scored rebase whose receipt names a local seal fails `V-SEAL-ORDER`: a receipt cannot bind a local seal.

A full rebase gets every check below. Without git history or `--objects`, the git and near-overlap checks are skipped, not passed; `--strict` makes a skip a failure. These are all the codes verify emits:

| Code | The check fails when |
|---|---|
| `V-MANIFEST-MISSING`, `V-MANIFEST-MALFORMED` | A `MANIFEST.sha256` is absent or a line is not `<sha256>  <path>`. |
| `V-HASH-MISSING`, `V-HASH-MISMATCH` | A listed file is absent or its bytes do not match. Also: the sealed MinHash object does not match the raw holdout inputs. |
| `V-MANIFEST-INCOMPLETE` | The rebase manifest leaves out a bound file, or the evidence manifest leaves out an evidence file or the rebase manifest. |
| `V-LOCK-MISSING` | `roadbed.lock` is absent. |
| `V-LOCK-INCOMPLETE` | The lock does not list every tracked file and bucket object, or a size or hash differs. |
| `V-PATH-UNSAFE` | A path is a symlink or points outside the asset. |
| `V-SCHEMA-INVALID` | A file or row does not validate against its schema (reader form; closed form with `--strict`). |
| `V-SYNTHETIC-FLAG` | A synthetic asset has a file or row without `synthetic: true`, a `SYNTHETIC.md` is missing (rule 9), or a mock or replay receipt is not synthetic. |
| `V-PII` | A file holds an email address. |
| `V-SIG-MISSING`, `V-SIG-INVALID`, `V-SIG-UNTRUSTED` | A receipt or evidence manifest has no signature, a bad one, or one by a key not in `--trust-key`. |
| `V-RECEIPT-CHAIN` | `prev_receipt` does not chain the receipts by `started_at`. |
| `V-RECEIPT-MISMATCH` | The receipt names predictions, a recipe, a snapshot or a holdout that is not the one in the asset. |
| `V-POLICY-MISMATCH` | `gate.json` `policy_sha256` is not `sha256(JCS(policy/gate.yaml))`. |
| `V-SCORES-MISMATCH` | `scores.json` differs from the recomputed scores. |
| `V-SEAL-AFTER-REBASE` | The holdout was not sealed before the rebase was created. Day 0, full and engine-scored rebases also fail when both times are equal. A local rebase passes when both times are in the same second. |
| `V-SEAL-INCOMPLETE` | Row hashes, Merkle roots or snapshot parts do not re-hash to the sealed values. |
| `V-SEAL-ORDER` | A leg of "Seal precedes training" (audit seq, timestamp, git ancestry) fails, a receipt names a local seal, or a seal or prediction file changed after its commit. |
| `V-HOLDOUT-OVERLAP` | Train and holdout share a row hash or an input, or a pair is near (Jaccard ≥ threshold). |
| `V-CONTAMINATION-MISMATCH` | `contamination.json` or the gate's contamination check differs from what verify computed. |
| `V-SOD` | SoD-01 (the sealer requested the run) or SoD-02 (the requester approved). |
| `V-OBJECT-MISSING` | A bucket object the asset names is not in `--objects` (last check; without `--strict` a skip). |
| `V-CHECK-ERROR` | A check could not run (unreadable file, bad value). |

## Sign-off

- **One role per actor.** `evidence/approvals.json` keys `approvals` by actor id and holds each actor's latest decision. One actor holds one role, so the validator and the approver differ (SoD-03) by construction.
- **History.** `history` is append-only and keeps every decision (a reject, then an approve). `approvals[actor]` is the actor's latest entry in `history`.
- **Role and decision.** A validator records `validated`, `validated_with_findings` or `rejected`. An approver records `approved` or `rejected`. Schema and model enforce this.
- **SoD-02.** The requester does not approve. This needs `rebase.yaml` too, so the control plane and verify check it.

## Runner jobs and bucket I/O

- **Self-contained jobs.** An `ExecutorJob` holds everything the runner needs: the base, the recipe (path and sha256), every input as an object (oid and size), the bucket (`storage`), and the outputs the result must name. The runner asks the control plane for nothing else.
- **Inputs.** The runner reads inputs from `storage` by oid, at `objects/sha256/<oid[0:2]>/<oid[2:4]>/<oid>`. The recipe bytes have oid `recipe.sha256`. A raw (pseudonymised) input has oid `input_ref` without the `sha256:` prefix. Train jobs get the snapshot manifest and parts, plus the holdout row hashes and MinHash object, never holdout rows. Eval jobs also get `holdout.file`, the holdout rows.
- **What the receipt copies.** The runner fills the receipt from the job alone. A train job has `snapshot` and `holdout`. An eval job has `holdout` and an optional `snapshot` (the adapter's training snapshot, `DatasetRef`). A bench job has an optional `snapshot` (`DatasetRef`) and an optional `holdout` (`HoldoutSealRef`: refs only, never rows). The control plane sends `snapshot` for every eval job, so its receipt is valid. A bench receipt without them is valid.
- **Attest jobs.** An `ExecutorJob` with spec kind `rebase` (`AttestSpec`) signs the rebase receipt. It names the train, eval and bench receipts to bind (`receipts`, by oid), the receipt `subjects` (the adapter `oid:<hex>` and both prediction files by asset path), `receipt_path` (`rebases/N/receipt.json`), the optional `config` digests and `outputs: [receipt]`. The control plane runs attest jobs in process (`mock`, `replay`) or on the runner, which holds the receipt key. The output is the signed `rebases/N/receipt.json` with job kind `rebase`: it copies `config` and takes the base, recipe, snapshot, holdout and image from the train receipt.
- **Bench cost.** A bench job's `bench` artifact is a `cost.json` with `cap_eur` null. The control plane sets `cap_eur` from `policy/gate.yaml` (`cost_cap.eur`) and checks the train cost (gpu, price, gpu-hours, eur) against the train receipt before it stores the file.
- **Receipt path.** `ExecutorJob.receipt_path` is the asset path the receipt goes to (`versions/vN/receipt.json` or `rebases/N/receipt.json`). The runner puts it in `receipt.sig` `signed`.
- **Before training.** The runner checks the seal order (seal seq and time against the job start) and disjointness (snapshot row hashes against the holdout row hashes, MinHash). If either fails, it refuses the job.
- **Outputs.** The runner writes each output content-addressed to the same bucket, then posts a `JobResult` that names each one (`artifacts`: name, oid, size) plus the signed receipt. The control plane records the oids and writes the asset files (predictions, `model.json`, receipt) from them. The runner never writes to git.
- **Credentials.** `storage.credentials` names a credential the runner already holds. A job never carries a secret.
- **SoD-05 and the runner.** The runner service identity is the only non-validator that reads holdout rows, and only for eval jobs. Each read is audited with role `runner`.
- **Leases.** `GET /v1/jobs/next` returns a `JobLease` (job, `lease_id`, `attempt`, `max_attempts`, `lease_until`, `audit_seq`, `prev_receipt`). `audit_seq` is the audit seq of the job start event and `prev_receipt` is `sha256(JCS(previous receipt.json))` in the asset (null for the first). The runner copies both into the receipt predicate. They are optional in the schema only for compatibility (additive change); the control plane always sends them. Heartbeat and status extend the lease and return `LeaseState`, which carries `cancel_requested`. An expired lease re-queues the job with `attempt + 1`; after `max_attempts` the job fails. A status or result with a stale lease or another attempt gets 409.
- **Executor protocol.** `roadbed.contracts.Executor` is an async Protocol: `submit`, `status`, `result` (raises `JobNotFinished` while queued or running), `cancel` (a no-op on a finished job). The control plane implements it: `runner` is an adapter over the lease queue, and `mock` and `replay` run in process. `hf-jobs` and `together` are stubs. The GPU box only speaks HTTP.

## Amendments

The format `roadbed/v0.1` has these amendments. Each one is additive, except the two W01d relaxations (see "Compatibility within v0.x"). Day 0 is the format before W01: the scores, the gate and the Day 0 verify checks. [CHANGELOG.md](../CHANGELOG.md) lists the changes of each release.

| Id | What it adds |
|---|---|
| W01 | The full v0.1 contract: every schema, the fixtures, the conformance cases and the hashing vectors. |
| W01b | Optional fields for synthetic rows, job leases, eval and bench jobs, `receipt_path`, `task.text_field`, the exact MinHash input and the full table of verify codes. |
| W01c | Attest jobs, `config` digests on the receipt predicate, `verdict-record` with the server-set `actor`, bench `cost.json` and bounds for audit payloads. |
| W01d | The local seal (2026-11-25): `SEAL.json` `provenance`, `audit_seq` required unless the seal is local, and `data.holdout` optional until a holdout is sealed. |

**Known gaps.** The fixtures in `spec/examples` address inputs by the sha256 of the raw narrative bytes, not by Hashing rule 3. `roadbed verify` and `roadbed evals build` shingle the whole input text, not only the text field of "MinHash input, exactly". An input whose free text is only SDK text tokens (`<TEXT:tok_...>`) has no text to compare: `roadbed evals build` warns, and `roadbed verify` reports the near check as a named skip.

**Compatibility with Day 0.** Every Day 0 file stays valid: W01 adds fields as optional only, and new constraints (actor ids, timestamps, the seal field group, the gate rules, check names) accept every Day 0 file. `policy_sha256` leaves out null fields, so the new optional policy fields do not change Day 0 hashes. Day 0 rebases have no `evidence/` and no receipt, so their manifests stay valid under the Day 0 rule, and `roadbed verify` prints the same bytes for them as on Day 0.

## Tools

- `uv run python spec/tools/build_examples.py` rebuilds `examples/pass`, `examples/blocked`, their `.bundle` git histories and `conformance/vectors`. It needs `git`. The output is deterministic, and a test fails when the checked-in files are stale. The signing keys are derived from public strings: they are test keys, never use them for a real receipt.
- `uv run python spec/tools/build_conformance.py` rebuilds `conformance/valid`, `conformance/invalid` and `conformance/README.md`.
- `uv run python -c "from roadbed.contracts import export_schemas; export_schemas('spec/schemas/v0.1')"` regenerates the schemas.
