# Changelog

All notable changes to this project are in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The versions follow [Semantic Versioning](https://semver.org/). See [docs/VERSIONING.md](docs/VERSIONING.md).

## [Unreleased]

## [0.1.0] - TBD

### Added

- Spec v0.1 (`roadbed/v0.1`): the asset format, the JSON Schemas and the conformance cases.
- Engine: per-slice scoring, paired bootstrap, gate, contamination check, license policy, base model watch, labels resolver and eval builder.
- Offline verify with three modes: full, engine-scored and local.
- CLI commands: `new`, `sample`, `evals build`, `evals seal`, `verify`, `rebase`, `eval reproduce`, `audit verify`, `evidence verify`, `watch`, `status`, `init`, `seal`, `promote` and `rollback`.
- `roadbed new` scaffolds an asset repo with no holdout and no control plane. With `--synthetic` it also writes `SYNTHETIC.md` and `rebases/SYNTHETIC.md`.
- `roadbed evals seal` seals a holdout candidate on your machine. The seal has `provenance: local` and no audit seq.
- `roadbed verify data/holdout/<id>` checks one sealed holdout (seal mode). It refuses a symlink in the path.
- `roadbed verify rebases/N` has a local mode for a local seal. It names each check that a laptop cannot do as a skip. It also checks that the asset holds no email address and that every synthetic flag agrees with `roadbed.yaml`.
- `roadbed evals build --objects` also stores the raw training inputs, so verify can run the near check on a local asset.
- A synthetic `roadbed evals build` writes `synthetic: true` on every row.
- SDK: `HttpSink` and `JsonlSink`, with `init` and `init_local`. `JsonlSink` writes captured events to local JSONL files. No server is needed.
- Synthetic sample `acme-2k`. It ships inside the `roadbed` wheel with `stand_in_predictions.py`. `roadbed sample NAME DIR` copies it out. `roadbed sample --list` names the samples.
- Typing marker `py.typed` in both packages.
- Conformance cases `seal/local` and `roadbed/unsealed`.

### Changed

- Spec amendment W01d: `Seal.provenance` is a new optional field (`control_plane` or `local`).
- Spec amendment W01d: `Seal.audit_seq` is optional for local seals.
- Spec amendment W01d: `data.holdout` in `roadbed.yaml` is optional until the asset has a sealed holdout.
- `roadbed rebase --from-predictions` and `roadbed verify rebases/N` exit 2 on an asset with no sealed holdout.
- A receipt over a local seal fails `V-SEAL-ORDER`, not `V-CHECK-ERROR`.
- The package descriptions and the CLI help no longer say `(prototype)`.
- The spec, the CLI help, the schemas and the sample files use public wording only. `SPEC.md` has an "Amendments" section that defines W01, W01b, W01c and W01d.

### Fixed

- SDK: ids and pseudonymisation tokens can no longer look like a phone or card number. Ids are `ix_`, `vd_` or `oc_` plus 26 base32 characters. Neither ids nor tokens hold a run of 7 or more digits.
- The near-duplicate check no longer passes on inputs that the SDK tokenised (`text="tokenize"`). `roadbed evals build` warns and writes `near_check: not_applicable_tokenised` in `build.json`. `roadbed verify` reports the near check as a named skip, so `--strict` fails.
- The quickstart in the READMEs runs as written. The sealer is `vik`, not `<you>`, which the shell read as a redirect. The quickstart test runs the README commands verbatim.
- The free path in the `roadbed` package README has the `cd`, `git init` and commit steps, so it runs.
- The build command in the SDK README has `--as-of` and runs inside the asset.
- "Your own verdicts" in the README and the SDK docs set `keep=("route",)` and read the tenant key from `ROADBED_TENANT_KEY`. They say that each string field of the model output goes in `keep` and that slice values must be short and lowercase.
- When every row is quarantined, `roadbed evals build` names the quarantine count and the first reason.
- The run line of `examples/sdk/openai_local_sink.py` sets `ROADBED_TENANT_KEY`.

### Removed

- The unused `huggingface_hub` dependency.

### Security

- `roadbed evals build` refuses an `--objects` store inside the asset. The store holds raw inputs, and these must not go into git.
- The CLI refuses an asset that holds a symlink. `roadbed verify` fails it with `V-PATH-UNSAFE` in every mode. Writes of the eval files, the seal files and `roadbed.yaml` never follow a symlink.
- Git calls set `log.showSignature=false`. A hostile `.git/config` can no longer run a program through `git log`.
- The CLI, `roadbed verify` included, refuses a `roadbed.yaml` gate, scorers, slices or output schema path that leaves the asset. The error does not show the file.
- The gate policy schema has upper limits: `n_boot` 1000000, `num_perm` 4096 and `shingle` 64.
- SDK: `JsonlSink` sets a file that exists to mode 0600 and its directory to 0700.
- SDK: `subject_token` returns None for a value that it cannot hash. It does not raise an error into the host.
- `roadbed evals build --holdout-out` refuses a directory that is not empty before it writes. It changes the mode only of a directory that it made.
- `roadbed new` and `roadbed sample` refuse a dangling symlink. The copy-paste hints quote the path.
- CI: `uv run --locked`, a gitleaks image pinned by digest, a ruff hook pinned by commit, and publish only from a commit on `main`.
- Gitleaks scans the tests and the spec. The allowlist names synthetic values, not paths.
- Version floors on the `roadbed` runtime dependencies.

### Notes

- This is the first public release.
- The format can change before 1.0.

[Unreleased]: https://github.com/roadbedai/roadbed/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/roadbedai/roadbed/releases/tag/v0.1.0
