"""Outage then replay loses nothing; ids dedupe in the spool and on the server; batches respect the 500 limit."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import openai
import respx

import roadbed_sdk
from conftest import API, GATEWAY, TEST_KEY, TRIAGE, FakeIngest, chat_completion

MSG = [{"role": "user", "content": "Claim 1: hail damage to the roof."}]


def _init(spool_path: Path, **kw: object) -> roadbed_sdk.Recorder:
    return roadbed_sdk.init(API, "test-token", TEST_KEY, "insurer-default@v1", spool_path=spool_path, background=False, **kw)  # type: ignore[arg-type]


def _calls(mock: respx.MockRouter, oai: openai.OpenAI, n: int) -> list[str]:
    mock.post(f"{GATEWAY}/chat/completions").mock(return_value=httpx.Response(200, json=chat_completion(TRIAGE)))
    client = roadbed_sdk.wrap_openai(oai)
    ids = []
    for _ in range(n):
        with roadbed_sdk.capture() as cap:
            client.chat.completions.create(model="m", messages=MSG)
        assert cap.interaction_id
        ids.append(cap.interaction_id)
    return ids


def test_outage_then_replay_loses_nothing(mock: respx.MockRouter, ingest: FakeIngest, oai: openai.OpenAI, spool_path: Path) -> None:
    _init(spool_path)
    ingest.down = True
    ids = _calls(mock, oai, 30)
    vids = [roadbed_sdk.verdict(i, "approve") for i in ids]
    oids = [roadbed_sdk.outcome(i, {"paid": True}, observed_at="2026-11-01T10:00:00Z") for i in ids]
    counts = roadbed_sdk.flush()
    assert ingest.interactions == {}
    assert (counts["interaction"], counts["verdict"], counts["outcome"]) == (30, 30, 30)

    # The host process restarts during the outage: the spool survives on disk.
    roadbed_sdk.shutdown()
    _init(spool_path)
    assert roadbed_sdk.flush()["interaction"] == 30

    ingest.down = False
    counts = roadbed_sdk.flush()
    assert set(ingest.interactions) == set(ids)
    assert set(ingest.verdicts) == set(vids)
    assert set(ingest.outcomes) == set(oids)
    assert all(v == 0 for v in counts.values())
    roadbed_sdk.shutdown()


def test_lost_ack_is_resent_and_deduplicated_by_id(mock: respx.MockRouter, ingest: FakeIngest, oai: openai.OpenAI, spool_path: Path) -> None:
    _init(spool_path)
    ids = _calls(mock, oai, 5)
    ingest.lose_acks = 1  # the server stores the batch, the answer never arrives
    assert roadbed_sdk.flush()["interaction"] == 5
    assert set(ingest.interactions) == set(ids)
    posts_before = ingest.posts
    assert roadbed_sdk.flush()["interaction"] == 0  # resent with the same ids, acked as duplicates
    assert ingest.posts == posts_before + 1
    assert len(ingest.interactions) == 5
    roadbed_sdk.shutdown()


def test_duplicate_ids_are_spooled_once(ingest: FakeIngest, spool_path: Path) -> None:
    _init(spool_path)
    assert roadbed_sdk.verdict("ix_1", "approve", verdict_id="vd_same") == "vd_same"
    assert roadbed_sdk.verdict("ix_1", "reject", verdict_id="vd_same") == "vd_same"
    rec = roadbed_sdk.flush()
    assert rec["verdict"] == 1  # ix_1 is unknown to the server (404): kept for later, not lost
    ingest.interactions["ix_1"] = {}
    roadbed_sdk.flush()
    assert list(ingest.verdicts) == ["vd_same"]
    assert ingest.verdicts["vd_same"]["action"] == "approve"  # the first write wins
    roadbed_sdk.shutdown()


def test_flush_batches_at_most_500(mock: respx.MockRouter, ingest: FakeIngest, spool_path: Path) -> None:
    rec = _init(spool_path)
    for i in range(1201):
        rec.spool.put("interaction", f"ix_{i}", _body(f"ix_{i}"))
    roadbed_sdk.flush()
    sizes = [len(c.request.content) > 0 for c in mock.routes[0].calls]
    assert len(sizes) == 3  # 500 + 500 + 201
    assert len(ingest.interactions) == 1201
    roadbed_sdk.shutdown()


def test_batch_422_falls_back_to_single_posts(mock: respx.MockRouter, ingest: FakeIngest, spool_path: Path) -> None:
    rec = _init(spool_path)
    mock.post(f"{API}/v1/interactions:batch").mock(return_value=httpx.Response(422, json={"status": 422}))
    for i in range(3):
        rec.spool.put("interaction", f"ix_{i}", _body(f"ix_{i}"))
    roadbed_sdk.flush()
    assert len(ingest.interactions) == 3
    roadbed_sdk.shutdown()


def test_server_422_keeps_the_row_out_of_the_loop(mock: respx.MockRouter, ingest: FakeIngest, spool_path: Path) -> None:
    rec = _init(spool_path)
    mock.post(f"{API}/v1/verdicts").mock(return_value=httpx.Response(422, json={"status": 422}))
    roadbed_sdk.verdict("ix_1", "approve", verdict_id="vd_1")
    counts = roadbed_sdk.flush()
    assert counts["verdict"] == 0 and counts["verdict_rejected"] == 1  # kept, not deleted, not resent
    assert rec.spool.due("verdict", 10, force=True) == []
    roadbed_sdk.shutdown()


def test_background_thread_flushes(mock: respx.MockRouter, ingest: FakeIngest, oai: openai.OpenAI, spool_path: Path) -> None:
    roadbed_sdk.init(API, "test-token", TEST_KEY, "insurer-default@v1", spool_path=spool_path, flush_interval=0.05)
    ids = _calls(mock, oai, 3)
    deadline = time.monotonic() + 5
    while set(ingest.interactions) != set(ids) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert set(ingest.interactions) == set(ids)
    roadbed_sdk.shutdown()


def _body(ix: str) -> str:
    import json

    return json.dumps({
        "interaction_id": ix, "model": "m", "version": "v1", "ts": "2026-11-01T10:00:00Z",
        "input_ref": "sha256:" + "0" * 64, "input": {}, "output": {}, "profile_id": "p@v1", "hmac_key_id": "hk_x",
    })
