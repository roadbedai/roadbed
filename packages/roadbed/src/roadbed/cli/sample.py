"""roadbed sample: copy a sample event stream out of the package, so a pip install needs no clone."""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Annotated

import typer
from rich.text import Text

from roadbed.cli.asset import AssetError, find_root
from roadbed.cli.output import EXIT_OK, EXIT_USAGE, console, error, print_json
from roadbed.samples import copy_sample, list_samples


def _fail(msg: str, as_json: bool) -> typer.Exit:
    if as_json:
        print_json({"ok": False, "error": msg, "exit_code": EXIT_USAGE})
    else:
        error(msg)
    return typer.Exit(EXIT_USAGE)


def sample(
    name: Annotated[str | None, typer.Argument(help="Sample name, for example acme-2k.")] = None,
    dest: Annotated[Path | None, typer.Argument(help="New directory to copy the sample into.")] = None,
    list_: Annotated[bool, typer.Option("--list", help="Print the sample names and stop.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print one JSON object instead of text.")] = False,
) -> None:
    """Copy a synthetic sample event stream into a new directory. No network, no model."""
    names = list_samples()
    if list_:
        if as_json:
            print_json({"ok": True, "samples": names, "exit_code": EXIT_OK})
        else:
            console().print("\n".join(names))
        return
    if name is None or dest is None:
        raise _fail("give a sample name and a new directory, or use --list", as_json)
    if name not in names:
        raise _fail(f"no sample named {name}; samples: {', '.join(names)}", as_json)
    if os.path.lexists(dest):  # also a dangling symlink: resolve() would follow it
        raise _fail(f"{dest} exists; give a new directory", as_json)
    out = dest.resolve()
    if out.exists():
        raise _fail(f"{dest} exists; give a new directory", as_json)
    try:
        find_root(out.parent)
    except AssetError:
        pass
    else:
        raise _fail(f"{dest} is inside an asset; copy the sample outside the asset", as_json)
    files = copy_sample(name, out)
    nxt = (f"roadbed new acme --model claims-triage --synthetic --slices {shlex.quote(f'{dest}/slices.yaml')} "
           f"--output-schema {shlex.quote(f'{dest}/output.schema.json')}")
    if as_json:
        print_json({"ok": True, "sample": name, "dir": str(out), "files": files, "next": nxt, "exit_code": EXIT_OK})
        return
    con = console()
    con.print(Text(f"copied {name} to {dest}:", style="dim"))
    for f in files:
        con.print(Text(f"  {f}"))
    con.print(Text("next:", style="dim"))
    con.print(Text(f"  {nxt}"))
