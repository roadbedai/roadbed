"""roadbed evals build: resolve captured events and write the frozen snapshot, golden set and correction tests.

The holdout candidate is written only with --holdout-out, to a directory outside the asset (the validator's).
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.text import Text

from roadbed.asset import ObjectStore
from roadbed.cli.asset import AssetError, find_root, open_asset
from roadbed.cli.output import EXIT_ERROR, EXIT_OK, console, error, print_json
from roadbed.evals import EvalPolicy, LeakageError, build, read_snapshots, write_candidate
from roadbed.evals.build import check_candidate_dir
from roadbed.labels import LABEL_RULES_V1, ResolveError, RulesNotPinned, resolve
from roadbed.labels import summary as resolve_summary

app = typer.Typer(name="evals", help="Build eval sets and the training snapshot from captured verdicts.", no_args_is_help=True)

EVENT_FILES = ("interactions", "verdicts", "outcomes")


def read_events(d: Path, name: str) -> list[dict[str, Any]]:
    """events/<name>.jsonl or .jsonl.gz; a missing outcomes file is empty."""
    for suffix, opener in ((".jsonl", open), (".jsonl.gz", gzip.open)):
        p = d / f"{name}{suffix}"
        if p.is_file():
            with opener(p, "rt", encoding="utf-8") as f:  # type: ignore[operator]
                return [json.loads(x) for x in f if x.strip()]
    if name == "outcomes":
        return []
    raise FileNotFoundError(f"{d}/{name}.jsonl not found")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@app.command("build")
def build_cmd(
    events: Annotated[Path, typer.Option("--events", help="Directory with interactions, verdicts, outcomes (.jsonl or .jsonl.gz).")],
    as_of: Annotated[str, typer.Option("--as-of", help="Cutoff, UTC YYYY-MM-DDTHH:MM:SSZ; the snapshot is S-<date>.")],
    seed: Annotated[int, typer.Option("--seed", help="Sampling seed for the holdout and golden draw (recorded).")] = 0,
    asset: Annotated[Path | None, typer.Option("--asset", help="Asset repo root (default: search up from cwd).")] = None,
    holdout_out: Annotated[Path | None, typer.Option("--holdout-out", help="Validator directory for the holdout candidate (outside the asset).")] = None,
    objects: Annotated[Path | None, typer.Option("--objects", help="Local bucket; snapshot parts are also stored there.")] = None,
    suffix: Annotated[str | None, typer.Option("--suffix", help="Snapshot id suffix: S-<date>-<suffix>.")] = None,
    parent: Annotated[str | None, typer.Option("--parent", help="Snapshot this one replaces.")] = None,
    created_at: Annotated[str | None, typer.Option("--created-at", help="Snapshot created_at (default: now, UTC).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print one JSON object instead of text.")] = False,
) -> None:
    """Resolve labels (label_rules@v1) and build the snapshot, golden set and correction tests."""
    try:
        a = open_asset(asset.resolve() if asset else find_root(Path.cwd()))
        if holdout_out:
            check_candidate_dir(holdout_out, a.root)
        schema = json.loads((a.root / a.cfg["task"]["output_schema"]).read_text(encoding="utf-8"))
        ints, vds, ocs = (read_events(events, n) for n in EVENT_FILES)
        rows = resolve(ints, vds, ocs, LABEL_RULES_V1, as_of=as_of, output_schema=schema)
        sealed: list[dict[str, Any]] = []
        hp = a.root / a.cfg["data"]["holdout"]["path"] / "holdout.jsonl"
        if hp.is_file():  # leave the sealed holdout out of everything; its rows are read, never printed
            sealed = [json.loads(x) for x in hp.read_text(encoding="utf-8").splitlines() if x.strip()]
        snaps = a.cfg["data"].get("verdicts") or "data/snapshots"
        store = ObjectStore(objects) if objects else None
        prior = read_snapshots(a.root, snaps, store)  # rows a model may have trained on: never holdout or golden
        out = build(rows, a.slices, EvalPolicy.from_gate(a.policy), seed, as_of=as_of, output_schema=schema,
                    created_at=created_at or _now(), sealed_holdout=sealed, trained_on=prior, suffix=suffix, parent=parent)
        written = out.write(a.root, snapshots_dir=snaps, store=store)
        cand = write_candidate(out.holdout_candidate, holdout_out, asset_root=a.root) if holdout_out else []
    except (AssetError, FileNotFoundError, FileExistsError, ValueError, ResolveError, RulesNotPinned, LeakageError) as e:
        if as_json:
            print_json({"ok": False, "error": str(e), "exit_code": EXIT_ERROR})
        else:
            error(str(e))
        raise typer.Exit(EXIT_ERROR)

    c = out.report["counts"]
    if as_json:
        print_json({"ok": True, "exit_code": EXIT_OK, "snapshot": out.snapshot.manifest, "resolve": resolve_summary(rows),
                    "report": out.report, "proof": out.proof, "files": written,
                    "holdout_candidate": {"rows": len(out.holdout_candidate), "written": [str(p) for p in cand]}})
        return
    con = console()
    if out.report["synthetic"]:
        con.print(Text("SYNTHETIC", style="bold") + Text(" tenant: illustrative data", style="dim"))
    root12 = out.snapshot.manifest["merkle_root"][:12]
    con.print(Text("snapshot ", style="bold") + Text(f"{out.snapshot.id}: {c['train']} rows, merkle {root12}, {LABEL_RULES_V1.version}"))
    con.print(f"golden {c['golden']}, correction tests {c['correction_tests']}, eval-only {c['eval_only']}, quarantined {c['quarantined']}")
    th = out.proof["pairs"]["train-holdout"]
    con.print(f"train vs holdout: 0 shared rows, 0 shared inputs, max Jaccard {th['max_jaccard']} (< {out.proof['purge_threshold']})")
    if cand:
        con.print(Text("holdout candidate ", style="bold") + Text(f"{len(out.holdout_candidate)} rows -> {holdout_out} (validator seals it)"))
    else:
        con.print(Text(f"holdout candidate {len(out.holdout_candidate)} rows not written; pass --holdout-out <validator dir>", style="dim"))
