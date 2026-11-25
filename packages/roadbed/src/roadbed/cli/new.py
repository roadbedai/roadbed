"""roadbed new: scaffold an asset repo on a laptop, with no holdout and no control plane.

Every file validates against its closed schema. Runs no git: it prints the line to commit the asset.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Annotated, Any

import jsonschema
import typer
from rich.text import Text

from roadbed.asset.files import AssetValidationError, validate
from roadbed.cli.output import EXIT_OK, EXIT_USAGE, console, error, print_json
from roadbed.io import read_yaml, write_yaml

SYNTHETIC_TEXT = ("# SYNTHETIC\n\nThis asset holds synthetic, illustrative data (roadbed new --synthetic).\n"
                  "No real tenant, reviewer or claim is in it.\n")


def scaffold(model: str, slices: dict[str, Any], output_schema: dict[str, Any], *, synthetic: bool, tenant: str,
             base: str, margin: float, min_n: int) -> dict[str, Any]:
    """The asset files (path -> document), each checked against its closed schema. Raises ValueError."""
    flag = {"synthetic": True} if synthetic else {}
    if slices.get("synthetic") is True and not synthetic:
        raise ValueError("the slices file says synthetic: true; pass --synthetic")
    jsonschema.Draft202012Validator.check_schema(output_schema)
    props = output_schema.get("properties")
    if output_schema.get("type") != "object" or not isinstance(props, dict) or not props:
        raise ValueError("the output schema must be an object schema with top-level properties (the scored fields)")
    docs: dict[str, Any] = {
        "roadbed.yaml": {
            "spec": "roadbed/v0.1", "model": model, "synthetic": synthetic, "tenant": tenant,
            "task": {"type": "structured-classification", "output_schema": "evals/output.schema.json", "text_field": "narrative"},
            "base": {"repo": base, "revision": "unpinned"},
            "data": {"verdicts": "data/snapshots"},
            "evals": {"scorers": "evals/scorers.yaml", "slices": "evals/slices.yaml", "margin": margin, "min_n": min_n,
                      "golden": "evals/golden.jsonl", "correction_tests": "evals/correction_tests.jsonl"},
            "recipe": "none", "gate": "policy/gate.yaml", "alias": {"name": f"{model}-prod", "version": "unset"},
            "license": "policy/license.yaml",
        },
        "policy/gate.yaml": {
            "margin": margin, "min_n": min_n, "ci": 0.95, "n_boot": 10000, "seed": 0,
            "block_on": ["slice_below_margin", "contamination", "license_denied"],
            "stages": {"shadow": {"stub": True}, "canary": {"stub": True}, "bias_tests": {"stub": True}},
            "contamination": {"minhash_threshold": 0.8, "num_perm": 128, "shingle": 3}, **flag,
        },
        "policy/license.yaml": {"allow": ["apache-2.0", "mit"], "default": "deny", **flag},
        "evals/slices.yaml": {**{k: v for k, v in slices.items() if k != "synthetic"}, **flag},
        "evals/scorers.yaml": {"type": "weighted_field_exact_match", "fields": [{"field": f, "weight": 1.0} for f in props], **flag},
    }
    names = {"roadbed.yaml": "roadbed", "policy/gate.yaml": "gate-policy", "policy/license.yaml": "license-policy",
             "evals/slices.yaml": "slices", "evals/scorers.yaml": "scorers"}
    for rel, doc in docs.items():
        try:
            validate(doc, names[rel], strict=True)
        except AssetValidationError as e:
            raise ValueError(f"{rel}: {e}") from None
    return docs


def new_cmd(
    path: Annotated[Path, typer.Argument(help="Directory to create. It must not exist.")],
    model: Annotated[str, typer.Option("--model", help="Name of the fine-tuned model this asset governs.")],
    slices: Annotated[Path, typer.Option("--slices", help="Slice keys and values (slices.yaml) to copy into evals/.")],
    output_schema: Annotated[Path, typer.Option("--output-schema", help="JSON Schema of one model output, copied into evals/.")],
    synthetic: Annotated[bool, typer.Option("--synthetic", help="Mark every file synthetic: true and write SYNTHETIC.md.")] = False,
    tenant: Annotated[str, typer.Option("--tenant", help="Display name of the tenant.")] = "unset",
    base: Annotated[str, typer.Option("--base", help="Hugging Face repo of the base model.")] = "unset",
    margin: Annotated[float, typer.Option("--margin", help="Most negative allowed slice delta, in score points.")] = -2.0,
    min_n: Annotated[int, typer.Option("--min-n", min=1, help="Slices with fewer rows never block.")] = 30,
    as_json: Annotated[bool, typer.Option("--json", help="Print one JSON object instead of text.")] = False,
) -> None:
    """Create a new asset repo: roadbed.yaml, the gate and license policies, slices, scorers and the output schema."""
    try:
        if os.path.lexists(path):  # also a dangling symlink, which mkdir would otherwise follow or fail on
            raise ValueError(f"{path} exists; roadbed new writes a new directory only")
        schema_bytes = output_schema.read_bytes()
        slices_doc, schema_doc = read_yaml(slices), json.loads(schema_bytes)
        if not isinstance(slices_doc, dict):
            raise ValueError(f"{slices}: expected a mapping with a keys field (slices.yaml)")
        if not isinstance(schema_doc, dict):
            raise ValueError(f"{output_schema}: expected a JSON Schema object")
        docs = scaffold(model, slices_doc, schema_doc, synthetic=synthetic, tenant=tenant, base=base, margin=margin, min_n=min_n)
    except (OSError, ValueError, jsonschema.SchemaError) as e:
        msg = e.message if isinstance(e, jsonschema.SchemaError) else str(e)
        if as_json:
            print_json({"ok": False, "error": msg, "exit_code": EXIT_USAGE})
        else:
            error(msg)
        raise typer.Exit(EXIT_USAGE) from None
    path.mkdir(parents=True)
    for rel, doc in docs.items():
        write_yaml(path / rel, doc)
    (path / "evals/output.schema.json").write_bytes(schema_bytes)
    files = [*docs, "evals/output.schema.json"]
    if synthetic:  # rule 9: a SYNTHETIC.md at the root and above the rebase files that roadbed rebase writes later
        (path / "rebases").mkdir()
        for rel in ("SYNTHETIC.md", "rebases/SYNTHETIC.md"):
            (path / rel).write_text(SYNTHETIC_TEXT, encoding="utf-8")
            files.append(rel)
    git_line = f'cd {shlex.quote(str(path))} && git init -q && git add -A && git commit -qm "asset"'
    if as_json:
        print_json({"ok": True, "dir": str(path), "files": files, "next": git_line, "exit_code": EXIT_OK})
        return
    con = console()
    con.print(Text("created ", style="bold") + Text(f"{path}: {len(files)} files" + (" (SYNTHETIC)" if synthetic else "")))
    con.print("no holdout yet: run roadbed evals build, then roadbed evals seal")
    con.print("base and alias read unset: edit them before a rebase")
    con.print(f"commit the asset: {git_line}")
