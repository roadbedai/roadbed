# `roadbed`

Roadbed: rebase a fine-tuned model onto a new open-weight base behind a per-slice gate.

**Usage**:

```console
$ roadbed [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--version`: Show version.
* `--json`: Every command prints one JSON object instead of text.
* `--help`: Show this message and exit.

**Commands**:

* `new`: Create a new asset repo: roadbed.yaml, the...
* `sample`: Copy a synthetic sample event stream into...
* `watch`: Report new Hugging Face Hub bases matching...
* `rebase`: Start a rebase on the control plane and...
* `verify`: Offline check: re-hash files, re-validate...
* `status`: Show model, alias, base and the latest...
* `init`: Write ~/.config/roadbed/config.toml: the...
* `seal`: Seal a stratified holdout on the control...
* `promote`: Point the alias at a rebase&#x27;s candidate.
* `rollback`: Point the alias back at the previous...
* `evals`: Build eval sets and the training snapshot...
* `eval`: Evaluation commands.
* `audit`: Audit log commands.
* `evidence`: Evidence pack commands.

## `roadbed new`

Create a new asset repo: roadbed.yaml, the gate and license policies, slices, scorers and the output schema.

**Usage**:

```console
$ roadbed new [OPTIONS] {path}
```

**Arguments**:

* `path`: Directory to create. It must not exist.  [required]

**Options**:

* `--model <str>`: Name of the fine-tuned model this asset governs.  [required]
* `--slices <path>`: Slice keys and values (slices.yaml) to copy into evals/.  [required]
* `--output-schema <path>`: JSON Schema of one model output, copied into evals/.  [required]
* `--synthetic`: Mark every file synthetic: true and write SYNTHETIC.md.
* `--tenant <str>`: Display name of the tenant.  [default: unset]
* `--base <str>`: Hugging Face repo of the base model.  [default: unset]
* `--margin <float>`: Most negative allowed slice delta, in score points.  [default: -2.0]
* `--min-n <int range>`: Slices with fewer rows never block.  [default: 30; x&gt;=1]
* `--json`: Print one JSON object instead of text.
* `--help`: Show this message and exit.

## `roadbed sample`

Copy a synthetic sample event stream into a new directory. No network, no model.

**Usage**:

```console
$ roadbed sample [OPTIONS] [name] [dest]
```

**Arguments**:

* `name`: Sample name, for example acme-2k.
* `dest`: New directory to copy the sample into.

**Options**:

* `--list`: Print the sample names and stop.
* `--json`: Print one JSON object instead of text.
* `--help`: Show this message and exit.

## `roadbed watch`

Report new Hugging Face Hub bases matching --base, pinned to a revision, with the license decision.

Each base is reported once (see --state). Inside an asset repo, policy/license.yaml decides
allow, review or deny.

**Usage**:

```console
$ roadbed watch [OPTIONS]
```

**Options**:

* `--base <str>`: Glob over Hub repo ids, e.g. &#x27;Qwen/Qwen3.*-27B&#x27;. The author part is literal.  [required]
* `--offline`: Read the 24 h cache only; no network.
* `--state <path>`: State file of detected bases (default: cache dir).
* `--asset <path>`: Asset repo root (default: search up from cwd).
* `--json`: Print one JSON object instead of text.
* `--help`: Show this message and exit.

## `roadbed rebase`

Start a rebase on the control plane and stream its events, or (with --from-predictions) score local predictions.

Remote: ends with &quot;opened rebase #N → awaiting MRM sign-off&quot; (exit 0) or &quot;blocked: &lt;slice&gt; &lt;delta&gt; ...&quot; (exit 4).
Local Day 0: scores supplied predictions on the sealed holdout, applies the gate, writes the rebase files.

**Usage**:

```console
$ roadbed rebase [OPTIONS] {model}
```

**Arguments**:

* `model`: Model name; must match roadbed.yaml in local mode.  [required]

**Options**:

* `--from-predictions <str>`: Local Day 0 mode: rebase directory holding predictions.{incumbent,candidate}.jsonl.
* `--base, --on <str>`: New base repo (local default: rebase.yaml, then last watch).
* `--revision <str>`: 40-hex revision of the new base (default: last watch).
* `--license <str>`: Local mode: SPDX license id of the new base.
* `--follow / --no-follow`: Remote mode: stream the timeline until sign-off or block.  [default: follow]
* `--interval <float range>`: Remote mode: seconds between polls.  [default: 2.0; x&gt;=0.0]
* `--timeout <float range>`: Remote mode: stop following after this many seconds (exit 3).  [default: 3600.0; x&gt;=0.0]
* `--asset <path>`: Asset repo root (default: search up from cwd).
* `--json`: Print one JSON object instead of text.
* `--help`: Show this message and exit.

## `roadbed verify`

Offline check: re-hash files, re-validate schemas, check signatures, seal order and disjointness, recompute every score.

A holdout directory gets the seal checks only. Without --trust-key, the key ids in config trust_keys (if any) are
the trusted keys.

**Usage**:

```console
$ roadbed verify [OPTIONS] {path}
```

**Arguments**:

* `path`: Rebase directory, e.g. rebases/13, or a sealed holdout, e.g. data/holdout/H-2026-11-02.  [required]

**Options**:

* `--asset <path>`: Asset repo root (default: search up from cwd).
* `--json`: Print one JSON object instead of text.
* `--objects <path>`: Local copy of the bucket (objects/sha256/ab/cd/&lt;oid&gt;).
* `--strict`: Fail on every skipped check; require the closed schemas.
* `--trust-key <str>`: Key id (sha256 of the raw ed25519 key) to accept. Repeatable.
* `--help`: Show this message and exit.

## `roadbed status`

Show model, alias, base and the latest rebases with their gate status (local asset repo, or --remote).

**Usage**:

```console
$ roadbed status [OPTIONS]
```

**Options**:

* `--asset <path>`: Asset repo root (default: search up from cwd).
* `--last <int>`: How many recent rebases to list.  [default: 5]
* `--remote`: Ask the control plane instead of reading the asset repo.
* `--model <str>`: Remote: model name (default: roadbed.yaml model).
* `--json`: Print one JSON object instead of text.
* `--help`: Show this message and exit.

## `roadbed init`

Write ~/.config/roadbed/config.toml: the control plane url, the token file and the trusted key ids.

**Usage**:

```console
$ roadbed init [OPTIONS]
```

**Options**:

* `--url <str>`: Control plane URL (https; http only for localhost).  [required]
* `--token-file <str>`: File that holds the API token (chmod 600).
* `--trust-key <str>`: Key id verify accepts. Repeatable.
* `--force`: Overwrite an existing config.
* `--check`: Then call GET /v1/models to test the url and token.
* `--json`: Print one JSON object instead of text.
* `--help`: Show this message and exit.

## `roadbed seal`

Seal a stratified holdout on the control plane (validators only, SoD-05). Prints the SEAL.json it wrote.

**Usage**:

```console
$ roadbed seal [OPTIONS]
```

**Options**:

* `--snapshot <str>`: Snapshot to draw from, e.g. S-2026-10-31.  [required]
* `--rows <int range>`: Holdout rows.  [x&gt;=1; required]
* `--seed <int range>`: Sampling seed.  [x&gt;=0; required]
* `--by <str>`: Slice key to stratify on. Repeatable.  [required]
* `--method <str>`: proportional or equal.  [default: proportional]
* `--model <str>`: Model (default: roadbed.yaml model).
* `--asset <path>`: Asset repo root (default: search up from cwd).
* `--json`: Print one JSON object instead of text.
* `--help`: Show this message and exit.

## `roadbed promote`

Point the alias at a rebase&#x27;s candidate. The control plane refuses (409) unless the gate passed and every
approval is in, and (403) for the requester or the validator (SoD-02, SoD-03).

**Usage**:

```console
$ roadbed promote [OPTIONS] {REBASE}
```

**Arguments**:

* `REBASE`: Rebase number whose candidate becomes the alias version.  [required]

**Options**:

* `--alias <str>`: Alias (default: roadbed.yaml alias.name).
* `--idempotency-key <str>`: Retry key (default: a new UUID).
* `--asset <path>`: Asset repo root (default: search up from cwd).
* `--json`: Print one JSON object instead of text.
* `--help`: Show this message and exit.

## `roadbed rollback`

Point the alias back at the previous version (or --to). Always audited with the reason code.

**Usage**:

```console
$ roadbed rollback [OPTIONS]
```

**Options**:

* `--reason <str>`: Machine reason code, e.g. incident_regression.  [required]
* `--to <str>`: Version to restore (default: the previous one).
* `--alias <str>`: Alias (default: roadbed.yaml alias.name).
* `--idempotency-key <str>`: Retry key (default: a new UUID).
* `--asset <path>`: Asset repo root (default: search up from cwd).
* `--json`: Print one JSON object instead of text.
* `--help`: Show this message and exit.

## `roadbed evals`

Build eval sets and the training snapshot from captured verdicts.

**Usage**:

```console
$ roadbed evals [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--help`: Show this message and exit.

**Commands**:

* `build`: Resolve labels (label_rules@v1) and build...
* `seal`: Seal the holdout candidate locally: write...

### `roadbed evals build`

Resolve labels (label_rules@v1) and build the snapshot, golden set and correction tests.

**Usage**:

```console
$ roadbed evals build [OPTIONS]
```

**Options**:

* `--events <path>`: Directory with interactions, verdicts, outcomes (.jsonl or .jsonl.gz).  [required]
* `--as-of <str>`: Cutoff, UTC YYYY-MM-DDTHH:MM:SSZ; the snapshot is S-&lt;date&gt;.  [required]
* `--seed <int>`: Sampling seed for the holdout and golden draw (recorded).  [default: 0]
* `--asset <path>`: Asset repo root (default: search up from cwd).
* `--holdout-out <path>`: Validator directory for the holdout candidate (outside the asset).
* `--objects <path>`: Local bucket; snapshot parts and raw training inputs are also stored there.
* `--suffix <str>`: Snapshot id suffix: S-&lt;date&gt;-&lt;suffix&gt;.
* `--parent <str>`: Snapshot this one replaces.
* `--created-at <str>`: Snapshot created_at (default: now, UTC).
* `--json`: Print one JSON object instead of text.
* `--help`: Show this message and exit.

### `roadbed evals seal`

Seal the holdout candidate locally: write holdout.jsonl and SEAL.json, put the MinHash object in --objects,
and point roadbed.yaml at the holdout. A local seal names a person and has no audit seq. It runs no git.

**Usage**:

```console
$ roadbed evals seal [OPTIONS]
```

**Options**:

* `--candidate <path>`: Directory that evals build --holdout-out wrote.  [required]
* `--sealed-by <str>`: User id of the person who seals (lowercase, no email).  [required]
* `--objects <path>`: Local bucket for the MinHash object (outside the asset).  [required]
* `--id <str>`: Holdout id (default: H-&lt;snapshot date&gt;).
* `--asset <path>`: Asset repo root (default: search up from cwd).
* `--json`: Print one JSON object instead of text.
* `--help`: Show this message and exit.

## `roadbed eval`

Evaluation commands.

**Usage**:

```console
$ roadbed eval [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--help`: Show this message and exit.

**Commands**:

* `reproduce`: Recompute scores.json from the two...

### `roadbed eval reproduce`

Recompute scores.json from the two prediction files with the stored seed, offline. Exit 4 on any difference.

**Usage**:

```console
$ roadbed eval reproduce [OPTIONS] {path}
```

**Arguments**:

* `path`: Rebase directory, e.g. rebases/13.  [required]

**Options**:

* `--asset <path>`: Asset repo root (default: search up from cwd).
* `--json`: Print one JSON object instead of text.
* `--help`: Show this message and exit.

## `roadbed audit`

Audit log commands.

**Usage**:

```console
$ roadbed audit [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--help`: Show this message and exit.

**Commands**:

* `verify`: Offline: check seq order, prev_hash links...

### `roadbed audit verify`

Offline: check seq order, prev_hash links and every event hash of an audit export, and the anchored heads.

**Usage**:

```console
$ roadbed audit verify [OPTIONS]
```

**Options**:

* `--export <path>`: Audit export (GET /v1/audit/export.jsonl), one event per line.  [required]
* `--anchor <path>`: audit/anchors/&lt;date&gt;.json to check the head against. Repeatable.
* `--strict`: Fail on every skipped check.
* `--json`: Print one JSON object instead of text.
* `--help`: Show this message and exit.

## `roadbed evidence`

Evidence pack commands.

**Usage**:

```console
$ roadbed evidence [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--help`: Show this message and exit.

**Commands**:

* `verify`: Offline: check the evidence manifest...

### `roadbed evidence verify`

Offline: check the evidence manifest signature and hashes, its coverage of the rebase, and approvals.json.

**Usage**:

```console
$ roadbed evidence verify [OPTIONS] {path}
```

**Arguments**:

* `path`: rebases/N/evidence (or rebases/N).  [required]

**Options**:

* `--asset <path>`: Asset repo root (default: search up from cwd).
* `--trust-key <str>`: Key id to accept. Repeatable. Default: config trust_keys.
* `--json`: Print one JSON object instead of text.
* `--help`: Show this message and exit.
