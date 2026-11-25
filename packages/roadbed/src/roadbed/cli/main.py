"""roadbed CLI: init, seal, watch, rebase, status, verify, eval reproduce, audit verify, evidence verify, promote, rollback.

Exit codes: 0 ok, 2 usage or bad local input, 3 remote error, 4 a check failed (or the gate blocked).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any

import jsonschema
import pydantic
import typer
from rich.table import Table
from rich.text import Text

from roadbed import __version__
from roadbed import watch as hub_watch
from roadbed.asset.verify import run_seal
from roadbed.cli import audit as audit_mod
from roadbed.cli import config as config_mod
from roadbed.cli import evidence as evidence_mod
from roadbed.cli import new as new_mod
from roadbed.cli import rebase as rebase_mod
from roadbed.cli import remote
from roadbed.cli import reproduce as reproduce_mod
from roadbed.cli import sample as sample_mod
from roadbed.cli import verify as verify_mod
from roadbed.cli import watch as watch_mod
from roadbed.cli.api import Api, ApiError, new_key, seg
from roadbed.cli.asset import (
    LICENSE_POLICY,
    NO_TRAINING_EXECUTORS,
    AssetError,
    find_root,
    open_asset,
    read_meta,
    rebase_dir,
)
from roadbed.cli.evals import app as evals_app
from roadbed.cli.output import (
    EXIT_OK,
    EXIT_REMOTE,
    EXIT_USAGE,
    EXIT_VERIFY_FAILED,
    STATUS_STYLE,
    Events,
    console,
    error,
    print_json,
    signed,
)
from roadbed.contracts import AliasConfig, Seal, Stratification
from roadbed.license_policy import load_policy

app = typer.Typer(
    name="roadbed",
    help="Roadbed: rebase a fine-tuned model onto a new open-weight base behind a per-slice gate.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
app.add_typer(evals_app, name="evals")
app.command("new")(new_mod.new_cmd)
app.command("sample")(sample_mod.sample)

AssetOpt = Annotated[Path | None, typer.Option("--asset", help="Asset repo root (default: search up from cwd).")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Print one JSON object instead of text.")]

WATCH_DECISION = {"allow": ("allowed", "green"), "review": ("needs review", "yellow"), "deny": ("denied", "red")}

INPUT_ERRORS = (AssetError, config_mod.ConfigError, OSError, ValueError, jsonschema.ValidationError, pydantic.ValidationError)


class _State:
    """Global options from the callback (`roadbed --json <command>`). Set on every invocation."""

    json = False


def _json(flag: bool) -> bool:
    return flag or _State.json


def _version(value: bool) -> None:
    if value:
        typer.echo(f"roadbed {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True, help="Show version.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Every command prints one JSON object instead of text.")] = False,
) -> None:
    _State.json = as_json


def _root(asset: Path | None) -> Path:
    return asset.resolve() if asset else find_root(Path.cwd())


def _trusted(flag: list[str] | None) -> list[str] | None:
    """--trust-key wins; else config trust_keys; else None (keys not pinned)."""
    if flag:
        return flag
    keys = config_mod.load().trust_keys
    return keys or None


def _local_cfg(asset: Path | None) -> dict[str, Any] | None:
    """roadbed.yaml of the asset repo we are in, or None outside one."""
    try:
        return open_asset(_root(asset)).cfg
    except AssetError:
        return None


def _fail(msg: str, as_json: bool, code: int = EXIT_USAGE, **extra: Any) -> typer.Exit:
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
    as_json = _json(as_json)
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
        raise _fail(f"no Hub repo matches {base}", as_json, EXIT_USAGE, base=base)
    except (hub_watch.BadGlob, hub_watch.StateError) as e:
        raise _fail(str(e), as_json, EXIT_USAGE, base=base)
    except hub_watch.WatchError as e:  # Hub unreachable, Hub error, no fresh cache
        raise _fail(str(e), as_json, EXIT_REMOTE, base=base)
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
    model: Annotated[str, typer.Argument(help="Model name; must match roadbed.yaml in local mode.")],
    from_predictions: Annotated[
        str | None,
        typer.Option("--from-predictions", help="Local Day 0 mode: rebase directory holding predictions.{incumbent,candidate}.jsonl."),
    ] = None,
    base: Annotated[str | None, typer.Option("--base", "--on", help="New base repo (local default: rebase.yaml, then last watch).")] = None,
    revision: Annotated[str | None, typer.Option("--revision", help="40-hex revision of the new base (default: last watch).")] = None,
    license_id: Annotated[str | None, typer.Option("--license", help="Local mode: SPDX license id of the new base.")] = None,
    follow: Annotated[bool, typer.Option("--follow/--no-follow", help="Remote mode: stream the timeline until sign-off or block.")] = True,
    interval: Annotated[float, typer.Option("--interval", min=0.0, help="Remote mode: seconds between polls.")] = 2.0,
    timeout: Annotated[float, typer.Option("--timeout", min=0.0, help="Remote mode: stop following after this many seconds (exit 3).")] = 3600.0,
    asset: AssetOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Start a rebase on the control plane and stream its events, or (with --from-predictions) score local predictions.

    Remote: ends with "opened rebase #N → awaiting MRM sign-off" (exit 0) or "blocked: <slice> <delta> ..." (exit 4).
    Local Day 0: scores supplied predictions on the sealed holdout, applies the gate, writes the rebase files.
    """
    as_json = _json(as_json)
    if from_predictions is None:
        _rebase_remote(model, base, revision, follow, interval, timeout, as_json)
        return
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
    code = EXIT_OK if res.gate.status == "pass" else EXIT_VERIFY_FAILED
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


def _api() -> Api:
    return Api(config_mod.load())


def _remote_fail(e: ApiError, as_json: bool, **extra: Any) -> typer.Exit:
    return _fail(str(e), as_json, EXIT_REMOTE, remote=e.to_dict(), **extra)


def _pinned_base(repo: str | None, revision: str | None) -> tuple[dict[str, str], str]:
    """--on/--revision, else the last `roadbed watch` detection. Returns (PinnedBase, trigger)."""
    trigger = "manual"
    if revision is None:
        try:
            last = watch_mod.cached(None)
        except watch_mod.NoCache:
            last = None
        if last is not None and (repo is None or last["repo"] == repo):
            repo, revision, trigger = last["repo"], last["sha"], "watch"
    if not repo:
        raise AssetError("no base: pass --on REPO --revision SHA, or run `roadbed watch` first")
    if not revision or not hub_watch.SHA.fullmatch(revision):
        raise AssetError(f"--revision must be the 40-hex commit of {repo} (or run `roadbed watch --base ...` first)")
    return {"repo": repo, "revision": revision}, trigger


def _rebase_remote(model: str, base: str | None, revision: str | None, follow: bool, interval: float, timeout: float,
                   as_json: bool) -> None:
    con = console()
    try:
        pinned, trigger = _pinned_base(base, revision)
        api = _api()
    except INPUT_ERRORS as e:
        raise _fail(str(e), as_json)
    printed: list[str] = []

    def on_event(e: dict[str, Any]) -> None:
        line = f"{remote.hhmmss(e['at'])}  {remote.event_text(e)}"
        printed.append(line)
        if not as_json:
            con.print(Text(remote.hhmmss(e["at"]) + "  ", style="dim") + Text(remote.event_text(e)))

    with api:
        try:
            view, resp = api.post(f"/v1/models/{seg(model)}/rebases", {"base": pinned, "trigger": trigger})
            n = view["rebase"]
            if not as_json:
                head = "existing rebase" if resp.status_code == 200 else "started rebase"
                con.print(Text(f"{head} #{n}", style="bold") + Text(f"  {model} → {pinned['repo']} @{pinned['revision'][:12]}"))
            if follow:
                got = remote.follow(api, view, on_event, interval=interval, timeout=timeout)
            else:
                got = remote.Followed(view)
        except ApiError as e:
            raise _remote_fail(e, as_json, events=printed)
        except (KeyError, TypeError, pydantic.ValidationError) as e:
            raise _fail(f"control plane response does not match the contract: {type(e).__name__}: {e}", as_json, EXIT_REMOTE)
    view = got.view
    try:
        summ = remote.summarize(view)
    except (KeyError, TypeError, pydantic.ValidationError) as e:
        raise _fail(f"control plane response does not match the contract: {type(e).__name__}: {e}", as_json, EXIT_REMOTE)

    if got.timed_out:
        code, final = EXIT_REMOTE, f"rebase #{view['rebase']} still {view['state']} after {timeout:g} s; check with `roadbed status --remote`"
    elif not follow:
        code, final = EXIT_OK, f"rebase #{view['rebase']}: {view['state']}"
    else:
        code = {"pass": EXIT_OK, "blocked": EXIT_VERIFY_FAILED, "failed": EXIT_REMOTE}.get(summ.status, EXIT_OK)
        final = summ.final
    if as_json:
        print_json({
            "ok": code == EXIT_OK,
            "rebase": view["rebase"],
            "model": view.get("model", model),
            "state": view["state"],
            "status": summ.status,
            "labels": summ.labels.to_dict(),
            "base": pinned,
            "overall": summ.scores.overall.model_dump() if summ.scores else None,
            "findings": [f.id for f in summ.gate.findings] if summ.gate else [],
            "pr": view.get("pr"),
            "events": got.events,
            "summary": summ.lines if follow else [],
            "final": final,
            "exit_code": code,
        })
        raise typer.Exit(code)
    if follow and not got.timed_out:
        for w in summ.labels.words:
            con.print(Text(w, style="bold"))
        for line in summ.lines:
            con.print(Text(line))
        con.print()
        style = STATUS_STYLE.get(summ.status, "")
        word, _, rest = final.partition(" ")
        con.print(Text(word, style=f"bold {style}".strip()) + Text(" " + rest, style="bold"))
    else:
        con.print(Text(final))
    raise typer.Exit(code)


# ---------------------------------------------------------------- verify


@app.command()
def verify(
    path: Annotated[str, typer.Argument(help="Rebase directory, e.g. rebases/13, or a sealed holdout, e.g. data/holdout/H-2026-11-02.")],
    asset: AssetOpt = None,
    as_json: JsonOpt = False,
    objects: Annotated[Path | None, typer.Option("--objects", help="Local copy of the bucket (objects/sha256/ab/cd/<oid>).")] = None,
    strict: Annotated[bool, typer.Option("--strict", help="Fail on every skipped check; require the closed schemas.")] = False,
    trust_key: Annotated[list[str] | None, typer.Option("--trust-key", help="Key id (sha256 of the raw ed25519 key) to accept. Repeatable.")] = None,
) -> None:
    """Offline check: re-hash files, re-validate schemas, check signatures, seal order and disjointness, recompute every score.

    A holdout directory gets the seal checks only. Without --trust-key, the key ids in config trust_keys (if any) are
    the trusted keys.
    """
    as_json = _json(as_json)
    con = console()
    checks: list[dict[str, Any]] = []
    executors: list[dict[str, str | None]] = []

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
        if c.code == "V-RECEIPT-CHAIN" and c.ok:
            for r in executors:
                kid = r["keyid"][:12] if r["keyid"] else "no readable signature"
                con.print(Text(f"receipt {r['receipt']}: executor {r['executor']}, signed by {kid}"))

    try:
        p = Path(path)
        a = open_asset(asset.resolve() if asset else find_root(p.resolve() if p.is_dir() else Path.cwd()))
        hdir = p if p.is_absolute() else a.root / p
        if not hdir.is_dir() and p.is_dir():
            hdir = p.resolve()
        if (hdir / "SEAL.json").is_file():
            _verify_seal(a, hdir.resolve(), on_check, checks, objects, strict, as_json, con)
            return
        d, n = rebase_dir(a.root, path)
        trusted = _trusted(trust_key)
    except INPUT_ERRORS as e:
        raise _fail(str(e), as_json)
    executors = verify_mod.receipt_executors(a.root) if verify_mod.is_full(a.root, d) else []
    mock = any(r["executor"] in NO_TRAINING_EXECUTORS for r in executors)

    def mock_line() -> None:
        if mock:
            con.print(Text("MOCK: no model was trained", style="bold"))

    try:
        verify_mod.run(a, d, n, on_check, objects=objects, strict=strict, trusted_keyids=trusted)
    except verify_mod.VerifyFailed as e:
        if as_json:
            print_json({"ok": False, "rebase": n, "failed": e.check.code, "detail": e.check.detail,
                        "checks": checks, "executors": executors, "mock": mock, "exit_code": EXIT_VERIFY_FAILED})
        else:
            con.print(Text("verify failed: ", style="bold red") + Text(e.check.code, style="bold"))
            mock_line()
        raise typer.Exit(EXIT_VERIFY_FAILED)
    if as_json:
        print_json({"ok": True, "rebase": n, "checks": checks, "executors": executors, "mock": mock, "exit_code": EXIT_OK})
    else:
        con.print(Text("verified ", style="bold green") + Text(f"rebase #{n}", style="bold"))
        mock_line()


def _verify_seal(a: Any, hdir: Path, on_check: Any, checks: list[dict[str, Any]], objects: Path | None, strict: bool, as_json: bool,
                 con: Any) -> None:
    """roadbed verify data/holdout/<id>: the checks of one sealed holdout (roadbed.asset.verify.run_seal)."""
    if not hdir.is_relative_to(a.root):
        raise ValueError(f"{hdir} is not inside the asset {a.root}")
    doc = json.loads((hdir / "SEAL.json").read_text(encoding="utf-8"))
    hid, local = doc.get("id"), doc.get("provenance") == "local"
    last = f"LOCAL seal: sealed by {doc.get('sealed_by')}; no audit chain; no role enforcement" if local else None
    try:
        run_seal(a, hdir, on_check, objects=objects, strict=strict)
    except verify_mod.VerifyFailed as e:
        if as_json:
            print_json({"ok": False, "holdout": hid, "local": local, "failed": e.check.code, "detail": e.check.detail,
                        "checks": checks, "exit_code": EXIT_VERIFY_FAILED})
        else:
            con.print(Text("verify failed: ", style="bold red") + Text(e.check.code, style="bold"))
        raise typer.Exit(EXIT_VERIFY_FAILED) from None
    if as_json:
        print_json({"ok": True, "holdout": hid, "local": local, "checks": checks, "exit_code": EXIT_OK})
        return
    con.print(Text("verified ", style="bold green") + Text(f"seal {hid}", style="bold"))
    if last:
        con.print(Text(last, style="bold"))


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
    remote_: Annotated[bool, typer.Option("--remote", help="Ask the control plane instead of reading the asset repo.")] = False,
    model: Annotated[str | None, typer.Option("--model", help="Remote: model name (default: roadbed.yaml model).")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Show model, alias, base and the latest rebases with their gate status (local asset repo, or --remote)."""
    as_json = _json(as_json)
    if remote_:
        _status_remote(asset, model, last, as_json)
        return
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


def _status_remote(asset: Path | None, model: str | None, last: int, as_json: bool) -> None:
    try:
        if model is None:
            cfg = _local_cfg(asset)
            if cfg is None:
                raise AssetError("pass --model, or run inside an asset repo")
            model = str(cfg["model"])
        api = _api()
    except INPUT_ERRORS as e:
        raise _fail(str(e), as_json)
    with api:
        try:
            detail = api.get(f"/v1/models/{seg(model)}")
            lines, rows = remote.model_lines(detail)
        except ApiError as e:
            raise _remote_fail(e, as_json)
        except (KeyError, TypeError) as e:
            raise _fail(f"control plane response does not match the contract: {type(e).__name__}: {e}", as_json, EXIT_REMOTE)
    rows = rows[:last] if last > 0 else []
    if as_json:
        print_json({"ok": True, "remote": True, "synthetic": bool(detail["summary"].get("synthetic")),
                    "model": detail["summary"], "rebases": rows, "exit_code": EXIT_OK})
        return
    con = console()
    con.print(Text(lines[0], style="bold"))
    for line in lines[1:]:
        con.print(Text(line))
    if not rows:
        con.print(Text("no rebases yet", style="dim"))
        return
    t = Table(box=None, show_edge=False, pad_edge=False, header_style="dim")
    for col in ("rebase", "state", "delta", "gate"):
        t.add_column(col, justify="right" if col in ("rebase", "delta") else "left")
    for r in rows:
        num, state, delta, gate = remote.rebase_row(r)
        t.add_row(num, Text(state, style=STATUS_STYLE.get(state, "")), delta, Text(gate, style=STATUS_STYLE.get(gate, "dim")))
    con.print(t)


# ---------------------------------------------------------------- init


@app.command()
def init(
    url: Annotated[str, typer.Option("--url", help="Control plane URL (https; http only for localhost).")],
    token_file: Annotated[str | None, typer.Option("--token-file", help="File that holds the API token (chmod 600).")] = None,
    trust_key: Annotated[list[str] | None, typer.Option("--trust-key", help="Key id verify accepts. Repeatable.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing config.")] = False,
    check: Annotated[bool, typer.Option("--check", help="Then call GET /v1/models to test the url and token.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Write ~/.config/roadbed/config.toml: the control plane url, the token file and the trusted key ids."""
    as_json = _json(as_json)
    p = config_mod.config_path()
    try:
        if p.exists() and not force:
            raise AssetError(f"{p} exists; pass --force to overwrite it")
        if token_file and not Path(token_file).expanduser().is_file():
            raise AssetError(f"token file not found: {token_file}")
        text = config_mod.render(url, token_file, trust_key or [])
        config_mod.write(p, text)
    except INPUT_ERRORS as e:
        raise _fail(str(e), as_json)
    models = None
    if check:
        try:
            with _api() as api:
                models = len(api.get("/v1/models", limit=50).get("items", []))
        except ApiError as e:
            raise _remote_fail(e, as_json, config=str(p))
        except INPUT_ERRORS as e:
            raise _fail(str(e), as_json)
    if as_json:
        print_json({"ok": True, "config": str(p), "url": config_mod.check_url(url), "token_file": token_file,
                    "trust_keys": trust_key or [], "checked": check, "models": models, "exit_code": EXIT_OK})
        return
    con = console()
    con.print(Text("wrote ", style="dim") + Text(str(p)))
    if check:
        con.print(Text(f"control plane OK: {models} models visible"))


# ---------------------------------------------------------------- seal


@app.command()
def seal(
    snapshot: Annotated[str, typer.Option("--snapshot", help="Snapshot to draw from, e.g. S-2026-10-31.")],
    rows: Annotated[int, typer.Option("--rows", min=1, help="Holdout rows.")],
    seed: Annotated[int, typer.Option("--seed", min=0, help="Sampling seed.")],
    by: Annotated[list[str], typer.Option("--by", help="Slice key to stratify on. Repeatable.")],
    method: Annotated[str, typer.Option("--method", help="proportional or equal.")] = "proportional",
    model: Annotated[str | None, typer.Option("--model", help="Model (default: roadbed.yaml model).")] = None,
    asset: AssetOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Seal a stratified holdout on the control plane (validators only, SoD-05). Prints the SEAL.json it wrote."""
    as_json = _json(as_json)
    try:
        if model is None:
            cfg = _local_cfg(asset)
            if cfg is None:
                raise AssetError("pass --model, or run inside an asset repo")
            model = str(cfg["model"])
        strat = Stratification.model_validate({"keys": by, "method": method})
        api = _api()
    except INPUT_ERRORS as e:
        raise _fail(str(e), as_json)
    body = {"model": model, "snapshot": snapshot, "rows": rows, "seed": seed,
            "stratification": strat.model_dump(mode="json", exclude_none=True)}
    with api:
        try:
            doc, _ = api.post("/v1/holdouts", body)
            s = Seal.model_validate(doc)
        except ApiError as e:
            raise _remote_fail(e, as_json)
        except pydantic.ValidationError as e:
            raise _fail(f"control plane response does not match the contract: {e}", as_json, EXIT_REMOTE)
    if as_json:
        print_json({"ok": True, "synthetic": s.synthetic, "seal": doc, "exit_code": EXIT_OK})
        return
    con = console()
    con.print(Text(remote.seal_line(s)))
    if s.synthetic:
        con.print(Text("SYNTHETIC data", style="bold"))


# ---------------------------------------------------------------- promote, rollback


def _alias(alias: str | None, asset: Path | None) -> str:
    if alias:
        return alias
    cfg = _local_cfg(asset)
    if cfg is None:
        raise AssetError("pass --alias, or run inside an asset repo")
    return str(cfg["alias"]["name"])


def _alias_call(path_verb: str, alias: str, body: dict[str, Any], key: str | None, as_json: bool) -> None:
    try:
        api = _api()
    except INPUT_ERRORS as e:
        raise _fail(str(e), as_json)
    key = key or new_key()
    with api:
        try:
            doc, resp = api.post(f"/v1/aliases/{seg(alias)}:{path_verb}", body, idempotency_key=key)
            cfg = AliasConfig.model_validate(doc)
            line = remote.alias_line(cfg, alias, resp.headers.get("etag"))
        except ApiError as e:
            raise _remote_fail(e, as_json, idempotency_key=key)
        except pydantic.ValidationError as e:
            raise _fail(f"control plane response does not match the contract: {e}", as_json, EXIT_REMOTE)
    if as_json:
        route = cfg.aliases[alias]
        print_json({"ok": True, "alias": alias, "version": route.version, "adapter": route.adapter,
                    "generation": cfg.generation, "etag": resp.headers.get("etag"), "idempotency_key": key, "exit_code": EXIT_OK})
        return
    console().print(Text(line))


@app.command()
def promote(
    rebase_n: Annotated[int, typer.Argument(metavar="REBASE", help="Rebase number whose candidate becomes the alias version.")],
    alias: Annotated[str | None, typer.Option("--alias", help="Alias (default: roadbed.yaml alias.name).")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key", help="Retry key (default: a new UUID).")] = None,
    asset: AssetOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Point the alias at a rebase's candidate. The control plane refuses (409) unless the gate passed and every
    approval is in, and (403) for the requester or the validator (SoD-02, SoD-03)."""
    as_json = _json(as_json)
    try:
        name = _alias(alias, asset)
    except INPUT_ERRORS as e:
        raise _fail(str(e), as_json)
    _alias_call("promote", name, {"rebase": rebase_n}, idempotency_key, as_json)


@app.command()
def rollback(
    reason: Annotated[str, typer.Option("--reason", help="Machine reason code, e.g. incident_regression.")],
    to_version: Annotated[str | None, typer.Option("--to", help="Version to restore (default: the previous one).")] = None,
    alias: Annotated[str | None, typer.Option("--alias", help="Alias (default: roadbed.yaml alias.name).")] = None,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key", help="Retry key (default: a new UUID).")] = None,
    asset: AssetOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Point the alias back at the previous version (or --to). Always audited with the reason code."""
    as_json = _json(as_json)
    try:
        name = _alias(alias, asset)
        if not re.fullmatch(r"[a-z_]{1,64}", reason):
            raise AssetError(f"--reason must be a reason code: lowercase letters and _ (got {reason!r})")
    except INPUT_ERRORS as e:
        raise _fail(str(e), as_json)
    body: dict[str, Any] = {"reason_code": reason}
    if to_version:
        body["to_version"] = to_version
    _alias_call("rollback", name, body, idempotency_key, as_json)


# ---------------------------------------------------------------- offline checks: eval, audit, evidence


def _check_printer(con: Any, as_json: bool, checks: list[dict[str, Any]]) -> Any:
    def on_check(c: Any) -> None:
        status = "ok" if c.ok else "skipped" if c.ok is None else "fail"
        checks.append({"code": c.code, "label": c.label, "status": status, "detail": c.detail})
        if as_json:
            return
        if c.ok is False:
            con.print(Text("FAIL ", style="bold red") + Text(c.code, style="bold") + Text(f"  {c.detail}"))
            return
        line = Text(c.label, style="dim" if c.ok is None else "")
        if c.detail:
            line.append(f"  ({c.detail})", style="dim")
        con.print(line)

    return on_check


eval_app = typer.Typer(help="Evaluation commands.", no_args_is_help=True)
audit_app = typer.Typer(help="Audit log commands.", no_args_is_help=True)
evidence_app = typer.Typer(help="Evidence pack commands.", no_args_is_help=True)
app.add_typer(eval_app, name="eval")
app.add_typer(audit_app, name="audit")
app.add_typer(evidence_app, name="evidence")


@eval_app.command("reproduce")
def eval_reproduce(
    path: Annotated[str, typer.Argument(help="Rebase directory, e.g. rebases/13, or a sealed holdout, e.g. data/holdout/H-2026-11-02.")],
    asset: AssetOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Recompute scores.json from the two prediction files with the stored seed, offline. Exit 4 on any difference."""
    as_json = _json(as_json)
    try:
        p = Path(path)
        a = open_asset(asset.resolve() if asset else find_root(p.resolve() if p.is_dir() else Path.cwd()))
        d, n = rebase_dir(a.root, path)
        res = reproduce_mod.reproduce(a, d, n)
    except INPUT_ERRORS as e:
        raise _fail(str(e), as_json)
    code = EXIT_OK if res.check.ok else EXIT_VERIFY_FAILED
    r = res.recomputed
    if as_json:
        print_json({
            "ok": bool(res.check.ok), "rebase": n, "synthetic": bool(a.cfg["synthetic"]), "executor": res.executor, "notice": r.notice,
            "seed": r.seed, "n_boot": r.n_boot, "ci": r.ci,
            "overall": r.overall.model_dump(), "slices": [s.model_dump() for s in r.slices],
            "check": {"code": res.check.code, "status": "ok" if res.check.ok else "fail", "detail": res.check.detail},
            "exit_code": code,
        })
        raise typer.Exit(code)
    con = console()
    if res.executor in ("mock", "replay"):
        con.print(Text(f"{res.executor.upper()} executor: these predictions did not come from a GPU run", style="bold"))
    elif res.executor is None:
        con.print(Text("no receipt: executor unknown", style="bold"))
    if a.cfg["synthetic"]:
        con.print(Text("SYNTHETIC data", style="bold"))
    if r.notice:
        con.print(Text(r.notice, style="dim"))
    con.print(Text(f"rebase #{n} · seed {r.seed} · {r.n_boot:,} resamples · {int(round(r.ci * 100))}% CI", style="dim"))
    t = Table(box=None, show_edge=False, pad_edge=False, header_style="dim")
    for col in ("slice", "n", "incumbent", "candidate", "delta", "CI", "flag"):
        t.add_column(col, justify="left" if col in ("slice", "CI", "flag") else "right")
    o = r.overall
    t.add_row("overall", f"{o.n:,}", f"{o.incumbent:.1f}", f"{o.candidate:.1f}", signed(o.delta),
              f"{signed(o.ci_low)} to {signed(o.ci_high)}", "")
    for s in r.slices:
        flag = "below margin" if s.below_margin else "low-n" if s.low_n else ""
        t.add_row(f"{s.key}={s.value}", f"{s.n:,}", f"{s.incumbent:.1f}", f"{s.candidate:.1f}", signed(s.delta),
                  f"{signed(s.ci_low)} to {signed(s.ci_high)}", flag)
    con.print(t)
    _check_printer(con, False, [])(res.check)
    raise typer.Exit(code)


@audit_app.command("verify")
def audit_verify(
    export: Annotated[Path, typer.Option("--export", help="Audit export (GET /v1/audit/export.jsonl), one event per line.")],
    anchor: Annotated[list[Path] | None, typer.Option("--anchor", help="audit/anchors/<date>.json to check the head against. Repeatable.")] = None,
    strict: Annotated[bool, typer.Option("--strict", help="Fail on every skipped check.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Offline: check seq order, prev_hash links and every event hash of an audit export, and the anchored heads."""
    as_json = _json(as_json)
    con = console()
    checks: list[dict[str, Any]] = []
    try:
        res = audit_mod.run(export, list(anchor or []), _check_printer(con, as_json, checks), strict=strict)
    except audit_mod.AuditFailed as e:
        if as_json:
            print_json({"ok": False, "failed": e.check.code, "detail": e.check.detail, "checks": checks,
                        "exit_code": EXIT_VERIFY_FAILED})
        else:
            con.print(Text("audit verify failed: ", style="bold red") + Text(e.check.code, style="bold"))
        raise typer.Exit(EXIT_VERIFY_FAILED)
    if as_json:
        print_json({"ok": True, "events": res.events, "first_seq": res.first_seq, "head_seq": res.head_seq,
                    "head_hash": res.head_hash, "denied": res.denied, "anchored": res.anchored, "synthetic_anchor": res.synthetic_anchor,
                    "checks": checks, "exit_code": EXIT_OK})
        return
    if res.synthetic_anchor:
        con.print(Text("SYNTHETIC anchor", style="bold"))
    head = f"head anchored by {res.anchored} anchor(s)" if res.anchored else "head not anchored"
    con.print(Text("audit verify OK", style="bold green") + Text(f"  {res.events:,} events, {res.denied:,} denied, {head}"))


@evidence_app.command("verify")
def evidence_verify(
    path: Annotated[Path, typer.Argument(help="rebases/N/evidence (or rebases/N).")],
    asset: AssetOpt = None,
    trust_key: Annotated[list[str] | None, typer.Option("--trust-key", help="Key id to accept. Repeatable. Default: config trust_keys.")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Offline: check the evidence manifest signature and hashes, its coverage of the rebase, and approvals.json."""
    as_json = _json(as_json)
    con = console()
    checks: list[dict[str, Any]] = []
    try:
        pack = evidence_mod.locate(path, asset)
        trusted = _trusted(trust_key)
    except INPUT_ERRORS as e:
        raise _fail(str(e), as_json)
    try:
        approvals = evidence_mod.run(pack, _check_printer(con, as_json, checks), trusted_keyids=trusted)
    except evidence_mod.EvidenceFailed as e:
        if as_json:
            print_json({"ok": False, "rebase": pack.rebase, "failed": e.check.code, "detail": e.check.detail,
                        "checks": checks, "exit_code": EXIT_VERIFY_FAILED})
        else:
            con.print(Text("evidence verify failed: ", style="bold red") + Text(e.check.code, style="bold"))
        raise typer.Exit(EXIT_VERIFY_FAILED)
    if as_json:
        print_json({"ok": True, "rebase": pack.rebase, "status": approvals["status"], "status_text": approvals["status_text"],
                    "pack_version": approvals["pack_version"], "synthetic": approvals["synthetic"], "checks": checks,
                    "exit_code": EXIT_OK})
        return
    if approvals.get("synthetic"):
        con.print(Text("SYNTHETIC data", style="bold"))
    con.print(Text("evidence verified ", style="bold green") + Text(f"rebase #{pack.rebase}", style="bold")
              + Text(f"  {approvals['status_text']}"))
