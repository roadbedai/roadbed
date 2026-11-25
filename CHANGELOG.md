# Changelog

All notable changes to this project are in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The versions follow [Semantic Versioning](https://semver.org/). See [docs/VERSIONING.md](docs/VERSIONING.md).

## [Unreleased]

### Fixed

- SDK: ids and pseudonymisation tokens can no longer look like a phone or card number. Ids are `ix_`, `vd_` or `oc_` plus 26 base32 characters. Neither ids nor tokens hold a run of 7 or more digits. Some tokens differ from earlier 0.1.0 pre-release builds.

## [0.1.0] - TBD

### Added

- Spec v0.1 (`roadbed/v0.1`): the asset format, the JSON Schemas and the conformance cases.
- Engine: per-slice scoring, paired bootstrap, gate, contamination check, license policy, base model watch, labels resolver and eval builder.
- Offline verify with three modes: full, engine-scored and local.
- CLI commands: `new`, `sample`, `evals build`, `evals seal`, `verify`, `rebase`, `eval reproduce`, `audit verify`, `evidence verify`, `watch`, `status`, `init`, `seal`, `promote` and `rollback`.
- SDK: `HttpSink` and `JsonlSink`, with `init` and `init_local`.
- Synthetic sample `acme-2k`.

### Changed

- Spec amendment W01d: `Seal.provenance` is a new optional field (`control_plane` or `local`).
- Spec amendment W01d: `Seal.audit_seq` is optional for local seals.
- Spec amendment W01d: `data.holdout` in `roadbed.yaml` is optional until the asset has a sealed holdout.

### Notes

- This is the first public release.
- The format can change before 1.0.

[Unreleased]: https://github.com/roadbedai/roadbed/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/roadbedai/roadbed/releases/tag/v0.1.0
