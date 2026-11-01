"""Outage then replay loses nothing; ids dedupe in the spool and on the server; batches respect the 500 limit."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx
import openai
import pytest
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


def test_a_non_finite_row_loses_only_itself(mock: respx.MockRouter, ingest: FakeIngest, oai: openai.OpenAI, spool_path: Path,
                                           caplog: pytest.LogCaptureFixture) -> None:
    # A row with NaN (from an older SDK, or any path that skipped the checks) must not stall the rows behind it.
    rec = _init(spool_path)
    poison = {"interaction_id": "ix_poison", "model": "m", "version": "v", "ts": "2026-11-01T10:00:00Z", "input_ref": "sha256:" + "0" * 64,
              "input": {}, "output": {"score": float("nan")}, "profile_id": "p@v1", "hmac_key_id": "hk_0123456789abcdef"}
    assert rec.spool.put("interaction", "ix_poison", json.dumps(poison))
    ingest.down = True
    ids = _calls(mock, oai, 3)
    assert roadbed_sdk.verdict(ids[0], "approve", {"score": float("nan")}) is not None  # mapped to null, recorded
    counts = roadbed_sdk.flush()  # the bad row is refused at encoding, before the network: even during the outage
    assert (counts["interaction"], counts["interaction_rejected"]) == (3, 1)
    ingest.down = False
    counts = roadbed_sdk.flush()
    assert set(ingest.interactions) == set(ids)
    assert len(ingest.verdicts) == 1
    assert (counts["interaction"], counts["interaction_rejected"]) == (0, 1)
    assert "ix_poison" in caplog.text and "not_json" in caplog.text
    roadbed_sdk.shutdown()


def test_spooling_refuses_non_finite_numbers(spool_path: Path) -> None:
    rec = _init(spool_path)
    from roadbed.contracts import OutcomeIn
    with pytest.raises(ValueError):
        rec._spool("outcome", OutcomeIn, {"outcome_id": "oc_1", "interaction_id": "ix_1", "outcome": {"x": float("inf")},
                                          "source": "csv", "observed_at": "2026-11-01T10:00:00Z"})
    assert rec.spool.counts()["outcome"] == 0
    roadbed_sdk.shutdown()


def test_a_first_attempt_duplicate_is_logged(mock: respx.MockRouter, ingest: FakeIngest, oai: openai.OpenAI, spool_path: Path,
                                             caplog: pytest.LogCaptureFixture) -> None:
    # The id was acked and deleted from the spool earlier; the host reuses it. The server keeps the first body.
    _init(spool_path)
    mock.post(f"{GATEWAY}/chat/completions").mock(return_value=httpx.Response(200, json=chat_completion(TRIAGE)))
    client = roadbed_sdk.wrap_openai(oai)
    with roadbed_sdk.capture(interaction_id="ix_reused_1"):
        client.chat.completions.create(model="m", messages=MSG)
    roadbed_sdk.flush()
    caplog.set_level(logging.WARNING, logger="roadbed_sdk")
    with roadbed_sdk.capture(interaction_id="ix_reused_1"):
        client.chat.completions.create(model="m", messages=MSG)
    assert roadbed_sdk.flush()["interaction"] == 0
    assert "interaction ix_reused_1 was already on the server" in caplog.text
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
