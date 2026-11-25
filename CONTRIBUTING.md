# Contributing

Thank you for your help. This file tells you how to make a change.

## Set up

You need Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```
uv sync
pre-commit install
```

Run the checks before you open a pull request:

```
uv run pytest -q
uv run --package roadbed-sdk pytest packages/roadbed-sdk/tests -q
uv run ruff check
uv run mypy
```

## Branch and pull request

1. Fork the repository and make a branch from `main`.
2. Make one change in each pull request.
3. Open the pull request. Fill in the checklist in the template.
4. Answer the review comments. A maintainer merges the pull request when CI passes.

## DCO sign-off

Each commit needs a sign-off. It says that you have the right to give the code under the licence of the project. There is no CLA.

```
git commit -s -m "Your message"
```

## Pull request checklist

- You wrote the tests first. A new test fails before your change and passes after it.
- You added one line under `Unreleased` in `CHANGELOG.md`.
- You regenerated the schemas if you changed `contracts.py`.
- You rebuilt the conformance cases and the examples if the format changed.
- You wrote the prose in Simplified Technical English.

## How to change the spec

The Pydantic models in `packages/roadbed/src/roadbed/contracts.py` are the source of the format.

1. Edit the model in `contracts.py`.
2. Regenerate `spec/schemas/v0.1`:

```
uv run python -c "from roadbed.contracts import export_schemas; export_schemas('spec/schemas/v0.1')"
```

3. Rebuild the conformance cases and the examples:

```
uv run python spec/tools/build_examples.py
uv run python spec/tools/build_conformance.py
```

4. Update `spec/SPEC.md`.
5. Open one pull request. Name the amendment in the title.

## Bugs and questions

Open a GitHub issue. Use the bug template for a bug. Use the spec question template for a question about the format. For a security problem, read [SECURITY.md](SECURITY.md) and do not open an issue.

## Support

Support is best effort. We answer each issue within one week.
