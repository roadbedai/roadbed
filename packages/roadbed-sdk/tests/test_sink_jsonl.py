"""The local sink: init_local writes the three event files a local `roadbed evals build --events DIR` reads. No network."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import httpx
import openai
import pytest
import respx

import roadbed_sdk
from conftest import GATEWAY, TEST_KEY, TRIAGE, chat_completion
from roadbed.cli.evals import read_events
from roadbed.contracts import InteractionIn, OutcomeIn, VerdictIn
from roadbed.labels import resolve
from roadbed_sdk import JsonlSink

MESSAGES = [{"role": "user", "content": "Claim by Jane Doe: rear-ended at a junction."}]
MODELS = {"interactions": InteractionIn, "verdicts": VerdictIn, "outcomes": OutcomeIn}


@pytest.fixture
def events(tmp_path: Path) -> Path:
    return tmp_path / "events"


@pytest.fixture
def local(events: Path, spool_path: Path):
    rec = roadbed_sdk.init_local(events, TEST_KEY, "insurer-default@v1", model="claims-triage", keep=("route", "line", "severity"),
                                 synthetic=True, spool_path=spool_path, background=False)
    yield rec
    roadbed_sdk.shutdown()


def _capture_one(mock: respx.MockRouter, oai: openai.OpenAI) -> str:
    mock.post(f"{GATEWAY}/chat/completions").mock(return_value=httpx.Response(200, json=chat_completion(TRIAGE)))
    client = roadbed_sdk.wrap_openai(oai)
    with roadbed_sdk.capture(subject="claimant-1", slices={"line": "auto"}) as cap:
        client.chat.completions.create(model="claims-triage-prod", messages=MESSAGES)
    assert cap.interaction_id
    return cap.interaction_id


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_init_local_makes_no_http_call(mock: respx.MockRouter, events: Path, local: roadbed_sdk.Recorder, oai: openai.OpenAI) -> None:
    _capture_one(mock, oai)
    gateway_calls = mock.calls.call_count
    roadbed_sdk.flush()
    assert mock.calls.call_count == gateway_calls  # only the model call; the flush sent nothing anywhere
    assert [c.request.url.host for c in mock.calls] == ["gw.roadbed.test"]
    assert (events / "interactions.jsonl").is_file()


def test_files_are_0600_in_a_0700_dir(mock: respx.MockRouter, events: Path, local: roadbed_sdk.Recorder, oai: openai.OpenAI) -> None:
    ix = _capture_one(mock, oai)
    roadbed_sdk.verdict(ix, "approve")
    roadbed_sdk.flush()
    assert stat.S_IMODE(os.stat(events).st_mode) == 0o700
    for name in ("interactions", "verdicts"):
        assert stat.S_IMODE(os.stat(events / f"{name}.jsonl").st_mode) == 0o600


def test_synthetic_is_on_every_written_interaction(mock: respx.MockRouter, events: Path, local: roadbed_sdk.Recorder, oai: openai.OpenAI) -> None:
    for _ in range(3):
        _capture_one(mock, oai)
    roadbed_sdk.flush()
    rows = _lines(events / "interactions.jsonl")
    assert len(rows) == 3 and all(r["synthetic"] is True for r in rows)


def test_lines_are_canonical_and_valid(mock: respx.MockRouter, events: Path, local: roadbed_sdk.Recorder, oai: openai.OpenAI) -> None:
    ix = _capture_one(mock, oai)
    roadbed_sdk.verdict(ix, "fix", {"severity": 3, "route": "fast_track", "line": "auto"}, reason_code="underestimated_injury")
    roadbed_sdk.flush()
    for name, model in MODELS.items():
        p = events / f"{name}.jsonl"
        if not p.exists():
            assert name == "outcomes"
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            body = json.loads(line)
            model.model_validate(body)
            assert line == json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def test_a_duplicate_appended_line_still_resolves(mock: respx.MockRouter, events: Path, local: roadbed_sdk.Recorder, oai: openai.OpenAI) -> None:
    ix = _capture_one(mock, oai)
    roadbed_sdk.verdict(ix, "approve")
    roadbed_sdk.flush()
    # A crash between append and spool ack re-appends the same line.
    for name in ("interactions", "verdicts"):
        p = events / f"{name}.jsonl"
        first = p.read_text(encoding="utf-8").splitlines()[0]
        with p.open("a", encoding="utf-8") as f:
            f.write(first + "\n")
    rows = resolve(read_events(events, "interactions"), read_events(events, "verdicts"), read_events(events, "outcomes"))
    assert len(rows) == 1


def test_a_refused_body_is_rejected_and_not_written(events: Path) -> None:
    bad = {"interaction_id": "ix_1", "model": "m", "version": "v1", "ts": "2026-11-01T10:00:00Z", "input_ref": "sha256:" + "0" * 64,
           "input": {}, "output": {}, "hmac_key_id": "hk_x"}  # no profile_id
    res = JsonlSink(events).write("interaction", [bad])
    assert res.rejected == {"ix_1": "profile_required"} and res.accepted == []
    assert not (events / "interactions.jsonl").exists() or (events / "interactions.jsonl").read_text() == ""


def test_end_to_end_read_events_returns_the_rows(mock: respx.MockRouter, events: Path, local: roadbed_sdk.Recorder, oai: openai.OpenAI) -> None:
    ix = _capture_one(mock, oai)
    vid = roadbed_sdk.verdict(ix, "approve")
    roadbed_sdk.flush()
    ints, vds = read_events(events, "interactions"), read_events(events, "verdicts")
    assert [r["interaction_id"] for r in ints] == [ix]
    assert [r["verdict_id"] for r in vds] == [vid]
    assert read_events(events, "outcomes") == []
    assert [r["interaction_id"] for r in resolve(ints, vds, [])] == [ix]
