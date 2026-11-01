"""The CLI surface: every command exists, takes --json, and the exit codes are 0, 2, 3, 4."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from roadbed.cli import output, remote
from roadbed.cli.main import app
from test_cli_fakeapi import Fixture

cli = CliRunner()
COMMANDS = [["init"], ["seal"], ["watch"], ["rebase"], ["status"], ["verify"], ["eval", "reproduce"], ["audit", "verify"],
            ["evidence", "verify"], ["promote"], ["rollback"]]


@pytest.mark.parametrize("cmd", COMMANDS, ids=" ".join)
def test_every_command_has_json(cmd: list[str]):
    r = cli.invoke(app, [*cmd, "--help"])
    assert r.exit_code == 0, r.output
    assert "--json" in r.output


def test_exit_codes():
    assert (output.EXIT_OK, output.EXIT_USAGE, output.EXIT_REMOTE, output.EXIT_VERIFY_FAILED) == (0, 2, 3, 4)


def test_unknown_command_exits_2():
    assert cli.invoke(app, ["deploy"]).exit_code == 2


def test_follow_polls_until_terminal_and_reports_each_event_once():
    views = Fixture("pass").views()
    served = iter(views[1:] + views[-1:])  # the server may repeat a view; events must not repeat
    calls: list[str] = []

    class FakeApi:
        def get(self, path: str):
            calls.append(path)
            return next(served)

    seen: list[str] = []
    got = remote.follow(FakeApi(), views[0], lambda e: seen.append(e["state"]), interval=0, timeout=60,  # type: ignore[arg-type]
                        sleep=lambda s: None, clock=lambda: 0.0)
    assert seen == [e["state"] for e in Fixture("pass").timeline]
    assert len(calls) == len(views) - 1 and not got.timed_out and got.view["state"] == "awaiting_signoff"


def test_follow_times_out():
    views = Fixture("pass").views()
    t = iter([0.0, 1.0, 2.0, 3.0, 4.0])

    class Stuck:
        def get(self, path: str):
            return views[2]

    got = remote.follow(Stuck(), views[0], lambda e: None, interval=1, timeout=3, sleep=lambda s: None,  # type: ignore[arg-type]
                        clock=lambda: next(t))
    assert got.timed_out and got.view["state"] == "seal_check" and len(got.events) == 3
