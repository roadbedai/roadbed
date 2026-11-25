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

from roadbed.asset import AssetValidationError, ObjectStore, sha256_file, validate
from roadbed.cli.asset import AssetError, find_root, open_asset
from roadbed.cli.output import EXIT_OK, EXIT_USAGE, EXIT_VERIFY_FAILED, console, error, print_json
from roadbed.evals import EvalPolicy, LeakageError, build, read_seen, read_snapshots, write_candidate
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


def read_tombstones(root: Path) -> set[str]:
    """Subject tokens in data/tombstones.jsonl (each line checked against tombstone-row). Their rows stay out."""
    p = root / "data" / "tombstones.jsonl"
    if not p.is_file():
        return set()
    tokens = set()
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            row = json.loads(line)
            try:
                validate(row, "tombstone-row", strict=True)
            except AssetValidationError as e:
                raise ValueError(f"data/tombstones.jsonl line {n}: {e}") from None
            tokens.add(row["subject_token"])
    return tokens


def read_sealed(root: Path, configured: str | None) -> tuple[list[dict[str, Any]], bool | None]:
    """Rows of every sealed holdout (data/holdout/*/ and the configured data.holdout.path) and the seals' synthetic
    flag (None without seals; seals that disagree stop the build). Each holdout.jsonl must match the sha256 in its
    SEAL.json, or the build stops. Rows are read, never printed."""
    dirs = {p.parent.resolve() for p in (root / "data" / "holdout").glob("*/holdout.jsonl")}
    if configured and (root / configured / "holdout.jsonl").is_file():
        dirs.add((root / configured).resolve())
    rows: list[dict[str, Any]] = []
    flags: set[bool] = set()
    for d in sorted(dirs):
        seal = d / "SEAL.json"
        if not seal.is_file():
            raise ValueError(f"{d.name}: holdout.jsonl without SEAL.json")
        doc = json.loads(seal.read_text(encoding="utf-8"))
        if sha256_file(d / "holdout.jsonl") != doc.get("sha256"):
            raise ValueError(f"{d.name}: holdout.jsonl does not match the sha256 in SEAL.json")
        flags.add(bool(doc.get("synthetic")))
        rows += [json.loads(x) for x in (d / "holdout.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    if len(flags) > 1:
        raise ValueError("the sealed holdouts disagree on the synthetic flag (one asset, one synthetic flag)")
    return rows, (flags.pop() if flags else None)


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
        erased = read_tombstones(a.root)  # SPEC.md: deletion is a tombstone plus a new snapshot without the rows
        rows = resolve(ints, vds, ocs, LABEL_RULES_V1, as_of=as_of, output_schema=schema, erased_subjects=erased)
        sealed, sealed_synthetic = read_sealed(a.root, (a.cfg["data"].get("holdout") or {}).get("path"))
        snaps = a.cfg["data"].get("verdicts") or "data/snapshots"
        store = ObjectStore(objects) if objects else None
        prior = read_snapshots(a.root, snaps, store)  # rows a model may have trained on: never holdout or golden
        seen = read_seen(a.root)  # rows ever in golden or correction tests (developer-visible): never holdout
        out = build(rows, a.slices, EvalPolicy.from_gate(a.policy), seed, as_of=as_of, output_schema=schema,
                    created_at=created_at or _now(), sealed_holdout=sealed, sealed_synthetic=sealed_synthetic, trained_on=prior,
                    seen=seen, suffix=suffix, parent=parent)
        written = out.write(a.root, snapshots_dir=snaps, store=store)
        cand = write_candidate(out.holdout_candidate, holdout_out, asset_root=a.root) if holdout_out else []
    except (AssetError, FileNotFoundError, FileExistsError, ValueError, ResolveError, RulesNotPinned, LeakageError) as e:
        # a leakage refusal is a failed check (exit 4, like roadbed verify); everything else is bad local input (exit 2)
        code = EXIT_VERIFY_FAILED if isinstance(e, LeakageError) else EXIT_USAGE
        if as_json:
            print_json({"ok": False, "error": str(e), "exit_code": code})
        else:
            error(str(e))
        raise typer.Exit(code)

    c = out.report["counts"]
    exact_only = out.proof["pairs"]["train-sealed"]["exact_only"]
    warnings = [f"{exact_only} sealed holdout rows have no input text in these events: train vs sealed is checked by id, "
                "row hash and input only for them (the gate's MinHash check still runs at rebase)"] if exact_only else []
    if as_json:
        print_json({"ok": True, "exit_code": EXIT_OK, "snapshot": out.snapshot.manifest, "resolve": resolve_summary(rows),
                    "erased_subjects": len(erased), "sealed_holdout_rows": len(sealed), "seen_rows": len(seen), "warnings": warnings,
                    "report": out.report, "proof": out.proof, "files": written,
                    "holdout_candidate": {"rows": len(out.holdout_candidate), "written": [str(p) for p in cand]}})
        return
    con = console()
    if out.report["synthetic"]:
        con.print(Text("SYNTHETIC", style="bold") + Text(" tenant: illustrative data", style="dim"))
    root12 = out.snapshot.manifest["merkle_root"][:12]
    con.print(Text("snapshot ", style="bold") + Text(f"{out.snapshot.id}: {c['train']} rows, merkle {root12}, {LABEL_RULES_V1.version}"))
    con.print(f"golden {c['golden']}, correction tests {c['correction_tests']}, eval-only {c['eval_only']}, quarantined {c['quarantined']}")
    for w in warnings:
        con.print(Text("warning ", style="bold yellow") + Text(w))
    th = out.proof["pairs"]["train-holdout"]
    con.print(f"train vs holdout: 0 shared rows, 0 shared inputs, max Jaccard {th['max_jaccard']} (< {out.proof['purge_threshold']})")
    if cand:
        seal_hint = "seal it: roadbed evals seal --candidate <dir>"
        con.print(Text("holdout candidate ", style="bold") + Text(f"{len(out.holdout_candidate)} rows -> {holdout_out} ({seal_hint})"))
    else:
        con.print(Text(f"holdout candidate {len(out.holdout_candidate)} rows not written; pass --holdout-out <validator dir>", style="dim"))
