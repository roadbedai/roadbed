"""Monochrome terminal output and number formatting for the CLI."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any

from rich.console import Console
from rich.text import Text

MINUS = "−"

# Exit codes
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_MATCH = 2
EXIT_BLOCKED = 3
EXIT_VERIFY_FAILED = 4

STATUS_STYLE = {"pass": "green", "blocked": "red", "fail": "red", "ok": "green"}


def console() -> Console:
    # highlight=False: no automatic colouring of numbers or paths. soft_wrap: never break event lines.
    return Console(file=sys.stdout, highlight=False, soft_wrap=True)


def signed(x: float) -> str:
    """One decimal, leading sign, Unicode minus. A value that rounds to zero prints as 0.0."""
    r = round(x, 1)
    if r == 0:
        return "0.0"
    return ("+" if r > 0 else MINUS) + f"{abs(r):.1f}"


def plain(x: float) -> str:
    """One decimal, Unicode minus for negatives, no plus sign (for margins)."""
    r = round(x, 1)
    return (MINUS if r < 0 else "") + f"{abs(r):.1f}"


def now_stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


class Events:
    """Collects event lines; prints them as a timeline unless JSON output is on."""

    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json
        self.lines: list[dict[str, str]] = []
        self.con = console()

    def emit(self, text: str, *, style: str | None = None) -> None:
        ts = now_stamp()
        self.lines.append({"t": ts, "event": text})
        if not self.as_json:
            line = Text(ts + "  ", style="dim")
            line.append(text, style=style)
            self.con.print(line)

    def final(self, text: str, status: str) -> None:
        """Final line, printed bare (no timestamp) so it can be matched exactly."""
        self.lines.append({"t": now_stamp(), "event": text})
        if not self.as_json:
            word, _, rest = text.partition(" ")
            line = Text(word, style=f"bold {STATUS_STYLE.get(status, '')}".strip())
            line.append(" " + rest, style="bold")
            self.con.print(line)


def print_json(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def error(msg: str) -> None:
    Console(file=sys.stderr, highlight=False, soft_wrap=True).print(Text("error: ", style="bold") + Text(msg))
