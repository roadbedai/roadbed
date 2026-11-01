"""roadbed CLI: watch, rebase, verify, status."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import jsonschema
import pydantic
import typer
from rich.table import Table
from rich.text import Text

from roadbed import __version__
from roadbed import watch as hub_watch
from roadbed.cli import rebase as rebase_mod
from roadbed.cli import verify as verify_mod
from roadbed.cli import watch as watch_mod
from roadbed.cli.asset import LICENSE_POLICY, AssetError, find_root, open_asset, read_meta, rebase_dir
from roadbed.cli.evals import app as evals_app
from roadbed.cli.output import (
    EXIT_BLOCKED,
    EXIT_ERROR,
    EXIT_NO_MATCH,
    EXIT_OK,
    EXIT_VERIFY_FAILED,
    STATUS_STYLE,
    Events,
    console,
    error,
    print_json,
    signed,
)
from roadbed.license_policy import load_policy

app = typer.Typer(
    name="roadbed",
    help="Roadbed (prototype): rebase a fine-tuned model onto a new open-weight base behind a per-slice gate.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
app.add_typer(evals_app, name="evals")

AssetOpt = Annotated[Path | None, typer.Option("--asset", help="Asset repo root (default: search up from cwd).")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Print one JSON object instead of text.")]

WATCH_DECISION = {"allow": ("allowed", "green"), "review": ("needs review", "yellow"), "deny": ("denied", "red")}

INPUT_ERRORS = (AssetError, FileNotFoundError, ValueError, jsonschema.ValidationError, pydantic.ValidationError)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"roadbed {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True, help="Show version.")] = False,
) -> None:
    pass


def _root(asset: Path | None) -> Path:
    return asset.resolve() if asset else find_root(Path.cwd())


def _fail(msg: str, as_json: bool, code: int = EXIT_ERROR, **extra: Any) -> typer.Exit:
    if as_json:
        print_json({"ok": False, "error": msg, "exit_code": code, **extra})
    else:
        error(msg)
    return typer.Exit(code)


# ---------------------------------------------------------------- watch


@app.command()
def watch(
    base: Annotated[str, typer.Option("--base", help="Glob over Hub repo ids, e.g. 'Qwen/Qwen3.*-27B'. The author part is literal.")],
    offline: Annotated[bool, typer.Option("--offline", help="Read the 24 h cache only; no network.")] = False,
    state: Annotated[Path | None, typer.Option("--state", help="State file of detected bases (default: cache dir).")] = None,
    asset: AssetOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Report new Hugging Face Hub bases matching --base, pinned to a revision, with the license decision.

    Each base is reported once (see --state). Inside an asset repo, policy/license.yaml decides
    allow, review or deny.
    """
    # License policy applies only inside an asset repo.
    policy = None
    try:
        root = _root(asset)
        if (root / LICENSE_POLICY).is_file():
            policy = load_policy(root / LICENSE_POLICY)
    except AssetError:
        pass
    except INPUT_ERRORS as e:
        raise _fail(f"{LICENSE_POLICY}: {e}", as_json)

    try:
        found = hub_watch.scan(base, state or watch_mod.state_path(), policy, offline=offline)
    except hub_watch.NoMatch:
        raise _fail(f"no Hub repo matches {base}", as_json, EXIT_NO_MATCH, base=base)
    except hub_watch.WatchError as e:
        raise _fail(str(e), as_json, base=base)
    if found:
        watch_mod.write_last(found[0])

    if as_json:
        print_json({"ok": True, "base": base, "detections": [d.to_dict() for d in found], "exit_code": EXIT_OK})
        return
    con = console()
    if not found:
        con.print(Text(f"no new base for {base}", style="dim"))
        return
    for d in found:
        line = Text("new base: ", style="bold") + Text(f"{d.repo} @{d.revision[:12]}")
        line.append(f"  license {d.license}")
        if d.decision:
            word, style = WATCH_DECISION[d.decision.decision]
            line.append(": ")
            line.append(word, style=style)
            line.append(" by policy")
        if d.source == "cache":
            line.append(f"  (cached {watch_mod.age(d.fetched_at)})", style="dim")
        con.print(line)


# ---------------------------------------------------------------- rebase


@app.command()
def rebase(
    model: Annotated[str, typer.Argument(help="Model name; must match roadbed.yaml.")],
    from_predictions: Annotated[
        str, typer.Option("--from-predictions", help="Rebase directory holding predictions.{incumbent,candidate}.jsonl.")
    ],
    base: Annotated[str | None, typer.Option("--base", help="New base repo (default: rebase.yaml, then last watch).")] = None,
    revision: Annotated[str | None, typer.Option("--revision", help="40-hex revision of the new base.")] = None,
    license_id: Annotated[str | None, typer.Option("--license", help="SPDX license id of the new base.")] = None,
    asset: AssetOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Score supplied predictions on the sealed holdout, apply the gate, and write the rebase files."""
    events = Events(as_json)
    try:
        a = open_asset(_root(asset))
        if model != a.cfg["model"]:
            raise AssetError(f"model {model!r} does not match roadbed.yaml model {a.cfg['model']!r}")
        d, n = rebase_dir(a.root, from_predictions)
        try:
            cached = watch_mod.cached(None)
        except watch_mod.NoCache:
            cached = None
        ref = rebase_mod.resolve_base(read_meta(d), base, revision, license_id, cached)
        res = rebase_mod.run(a, d, n, ref, events.emit)
    except INPUT_ERRORS as e:
        raise _fail(str(e), as_json, events=events.lines)

    events.final(res.final, res.gate.status)
    code = EXIT_OK if res.gate.status == "pass" else EXIT_BLOCKED
    if as_json:
        o = res.scores.overall
        print_json({
            "ok": True,
            "synthetic": res.scores.synthetic,
            "rebase": n,
            "status": res.gate.status,
            "exit_code": code,
            "base": {"repo": ref.repo, "revision": ref.revision, "license": ref.license},
            "overall": o.model_dump(),
            "findings": [f.id for f in res.gate.findings],
            "files": res.files,
            "events": events.lines,
            "final": res.final,
        })
    raise typer.Exit(code)


# ---------------------------------------------------------------- verify


@app.command()
def verify(
    path: Annotated[str, typer.Argument(help="Rebase directory, e.g. rebases/13.")],
    asset: AssetOpt = None,
    as_json: JsonOpt = False,
    objects: Annotated[Path | None, typer.Option("--objects", help="Local copy of the bucket (objects/sha256/ab/cd/<oid>).")] = None,
    strict: Annotated[bool, typer.Option("--strict", help="Fail on every skipped check; require the closed schemas.")] = False,
    trust_key: Annotated[list[str] | None, typer.Option("--trust-key", help="Key id (sha256 of the raw ed25519 key) to accept. Repeatable.")] = None,
) -> None:
    """Offline check: re-hash files, re-validate schemas, check signatures, seal order and disjointness, recompute every score."""
    con = console()
    checks: list[dict[str, Any]] = []

    def on_check(c: verify_mod.Check) -> None:
        status = "ok" if c.ok else "skipped" if c.ok is None else "fail"
        checks.append({"code": c.code, "label": c.label, "status": status, "detail": c.detail})
        if as_json:
            return
        if c.ok is False:
            line = Text("FAIL ", style="bold red") + Text(c.code, style="bold") + Text(f"  {c.detail}")
        else:
            line = Text(c.label, style="dim" if c.ok is None else "")
            if c.detail:
                line.append(f"  ({c.detail})", style="dim")
        con.print(line)

    try:
        p = Path(path)
        a = open_asset(asset.resolve() if asset else find_root(p.resolve() if p.is_dir() else Path.cwd()))
        d, n = rebase_dir(a.root, path)
    except INPUT_ERRORS as e:
        raise _fail(str(e), as_json)

    try:
        verify_mod.run(a, d, n, on_check, objects=objects, strict=strict, trusted_keyids=trust_key)
    except verify_mod.VerifyFailed as e:
        if as_json:
            print_json({"ok": False, "rebase": n, "failed": e.check.code, "detail": e.check.detail,
                        "checks": checks, "exit_code": EXIT_VERIFY_FAILED})
        else:
            con.print(Text("verify failed: ", style="bold red") + Text(e.check.code, style="bold"))
        raise typer.Exit(EXIT_VERIFY_FAILED)
    if as_json:
        print_json({"ok": True, "rebase": n, "checks": checks, "exit_code": EXIT_OK})
    else:
        con.print(Text("verified ", style="bold green") + Text(f"rebase #{n}", style="bold"))


# ---------------------------------------------------------------- status


def _rebases(root: Path) -> list[dict[str, Any]]:
    out = []
    base = root / "rebases"
    dirs = sorted((p for p in base.iterdir() if p.is_dir() and p.name.isdigit()), key=lambda p: int(p.name)) if base.is_dir() else []
    for d in dirs:
        row: dict[str, Any] = {"rebase": int(d.name), "status": "not run", "delta": None, "base": None, "findings": []}
        meta = read_meta(d) or {}
        if isinstance(meta.get("base"), dict):
            row["base"] = meta["base"].get("repo")
        try:
            gate = json.loads((d / "gate.json").read_text(encoding="utf-8"))
            row["status"] = gate["status"]
            row["findings"] = [f["id"] for f in gate.get("findings", [])]
            scores = json.loads((d / "scores.json").read_text(encoding="utf-8"))
            row["delta"] = scores["overall"]["delta"]
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass
        out.append(row)
    return out


@app.command()
def status(
    asset: AssetOpt = None,
    last: Annotated[int, typer.Option("--last", help="How many recent rebases to list.")] = 5,
    as_json: JsonOpt = False,
) -> None:
    """Show model, alias, base and the latest rebases with their gate status."""
    try:
        a = open_asset(_root(asset))
    except INPUT_ERRORS as e:
        raise _fail(str(e), as_json)
    cfg = a.cfg
    rows = _rebases(a.root)[-last:] if last > 0 else []
    if as_json:
        print_json({
            "ok": True,
            "synthetic": cfg["synthetic"],
            "model": cfg["model"],
            "tenant": cfg["tenant"],
            "alias": cfg["alias"],
            "base": cfg["base"],
            "rebases": rows,
        })
        return
    con = console()
    con.print(Text(cfg["model"], style="bold") + Text(f"  {cfg['tenant']}", style="dim"))
    con.print(Text("alias  ", style="dim") + Text(f"{cfg['alias']['name']} {cfg['alias']['version']}"))
    rev = cfg["base"]["revision"]
    con.print(Text("base   ", style="dim") + Text(f"{cfg['base']['repo']} @{rev[:12] if rev != 'unpinned' else rev}"))
    if cfg.get("synthetic"):
        con.print(Text("synthetic asset repo: illustrative data", style="dim"))
    if not rows:
        con.print(Text("no rebases yet", style="dim"))
        return
    t = Table(box=None, show_edge=False, pad_edge=False, header_style="dim")
    for col in ("rebase", "base", "delta", "gate", "findings"):
        t.add_column(col, justify="right" if col in ("rebase", "delta") else "left")
    for r in rows:
        t.add_row(
            f"#{r['rebase']}",
            r["base"] or "",
            signed(r["delta"]) if r["delta"] is not None else "",
            Text(r["status"], style=STATUS_STYLE.get(r["status"], "dim")),
            ", ".join(r["findings"]),
        )
    con.print(t)
