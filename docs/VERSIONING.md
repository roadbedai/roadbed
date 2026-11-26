# Versioning

## Packages

The packages `roadbed` and `roadbed-sdk` follow [Semantic Versioning](https://semver.org/). Both packages have the same version number in each release.

Before 1.0, a minor version can contain a breaking change. The changelog names each breaking change.

## Format

The format has its own version. The current version is `roadbed/v0.1`. Each asset names it in `roadbed.yaml`.

## Additive within 0.1.x

A change to the format in 0.1.x must be additive. Each file that is valid for an earlier 0.1.x reader stays valid for a later reader.

Before the release of 0.1.0, the amendment W01d made two fields optional:

- `Seal.audit_seq` is optional for a local seal.
- `data.holdout` in `roadbed.yaml` is optional until the asset has a sealed holdout.

0.1.0 is the first public version. Each later 0.1.x change must be additive.

## Deprecation before 1.0

Before 1.0, we deprecate a feature before we remove it. We give one minor version of warning. The warning is in the changelog and in the CLI output.

## Package version and spec version

The package version and the spec version are not the same number. Each package states which spec version it reads and writes. Package 0.1.x reads and writes `roadbed/v0.1`.
