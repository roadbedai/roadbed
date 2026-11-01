"""A wrapped call produces the right InteractionIn, linked to the gateway ids and to later verdicts."""

from __future__ import annotations

import asyncio
import hashlib
import json

import anthropic
import httpx
import openai
import pytest
import respx
import rfc8785

import roadbed_sdk
from conftest import API, GATEWAY, GATEWAY_HEADERS, TEST_KEY, TRIAGE, FakeIngest, anthropic_message, chat_completion
from roadbed.contracts import InteractionIn

MESSAGES = [
    {"role": "system", "content": "Triage the claim. Answer JSON."},
    {"role": "user", "content": "Claim by Jane Doe (jane.doe@example.com): rear-ended at a junction, neck pain."},
]
# Known answers from openssl (see test_pseudo.py), not from the code under test.
SUBJECT_TOKEN = "tok_81d2fde77376a307ab7dc235a061e649"
KEY_ID = "hk_b372bc33f7565e0c"


def _openai(mock: respx.MockRouter, headers: dict[str, str] | None = None) -> respx.Route:
    return mock.post(f"{GATEWAY}/chat/completions").mock(return_value=httpx.Response(200, json=chat_completion(TRIAGE), headers=headers or {}))


def test_wrapped_openai_call_produces_the_right_interaction(mock: respx.MockRouter, ingest: FakeIngest, sdk: roadbed_sdk.Recorder, oai: openai.OpenAI) -> None:
    route = _openai(mock, GATEWAY_HEADERS)
    client = roadbed_sdk.wrap_openai(oai)
    with roadbed_sdk.capture(subject="claimant-123", pii=["Jane Doe"], slices={"line": "auto"}) as cap:
        resp = client.chat.completions.create(model="claims-triage-prod", messages=MESSAGES)

    assert resp.choices[0].message.content == TRIAGE  # the host gets the model answer unchanged
    sent_id = route.calls.last.request.headers["x-roadbed-interaction-id"]
    assert sent_id.startswith("ix_")
    assert cap.interaction_id == "ix_gateway_0001"  # the gateway's id wins: it is the id in the gateway's log
    assert roadbed_sdk.last_interaction_id() == "ix_gateway_0001"

    roadbed_sdk.flush()
    assert list(ingest.interactions) == ["ix_gateway_0001"]
    body = ingest.interactions["ix_gateway_0001"]
    InteractionIn.model_validate(body)
    assert body["model"] == "claims-triage"
    assert body["alias"] == "claims-triage-prod"
    assert body["version"] == "v12"
    assert body["output"] == {"severity": 2, "route": "fast_track", "line": "auto"}
    assert body["profile_id"] == "insurer-default@v1"
    assert body["hmac_key_id"] == KEY_ID
    assert body["subject_token"] == SUBJECT_TOKEN
    assert body["slices"] == {"line": "auto"}
    assert body["synthetic"] is True
    assert body["input_ref"] == "sha256:" + hashlib.sha256(rfc8785.dumps(body["input"])).hexdigest()
    roles = [m["role"] for m in body["input"]["messages"]]
    assert roles == ["system", "user"]
    assert all(m["content"].startswith("<TEXT:tok_") for m in body["input"]["messages"])
    assert body["latency_ms"] >= 0

    # Linkage: a verdict on the captured id reaches the server against the same interaction.
    vid = roadbed_sdk.verdict(cap.interaction_id, "fix", {"severity": 3, "route": "fast_track", "line": "auto"}, reason_code="underestimated_injury")
    roadbed_sdk.flush()
    assert ingest.verdicts[vid]["interaction_id"] == "ix_gateway_0001"
    assert ingest.verdicts[vid]["verdict"] == {"severity": 3, "route": "fast_track", "line": "auto"}


def test_without_gateway_headers_the_sdk_id_and_response_model_are_used(
    mock: respx.MockRouter, ingest: FakeIngest, sdk: roadbed_sdk.Recorder, oai: openai.OpenAI,
) -> None:
    route = _openai(mock)
    client = roadbed_sdk.wrap_openai(oai)
    client.chat.completions.create(model="gpt-test", messages=MESSAGES)
    sent_id = route.calls.last.request.headers["x-roadbed-interaction-id"]
    roadbed_sdk.flush()
    body = ingest.interactions[sent_id]
    assert body["version"] == "claims-triage-v12"
    assert "alias" not in body


def test_host_id_is_used_once_then_fresh_ids(mock: respx.MockRouter, ingest: FakeIngest, sdk: roadbed_sdk.Recorder, oai: openai.OpenAI) -> None:
    _openai(mock)
    client = roadbed_sdk.wrap_openai(oai)
    with roadbed_sdk.capture(interaction_id="ix_host_1") as cap:
        client.chat.completions.create(model="m", messages=MESSAGES)
        client.chat.completions.create(model="m", messages=MESSAGES)
    assert cap.interaction_ids[0] == "ix_host_1"
    assert len(set(cap.interaction_ids)) == 2
    roadbed_sdk.flush()
    assert set(ingest.interactions) == set(cap.interaction_ids)


def test_wrapped_anthropic_call(mock: respx.MockRouter, ingest: FakeIngest, sdk: roadbed_sdk.Recorder, anth: anthropic.Anthropic) -> None:
    mock.post(f"{GATEWAY}/v1/messages").mock(return_value=httpx.Response(200, json=anthropic_message(TRIAGE), headers=GATEWAY_HEADERS))
    client = roadbed_sdk.wrap_anthropic(anth)
    with roadbed_sdk.capture(subject="claimant-123") as cap:
        resp = client.messages.create(model="claims-triage-prod", max_tokens=64, system="Triage.", messages=MESSAGES[1:])
    assert resp.content[0].text == TRIAGE
    roadbed_sdk.flush()
    body = ingest.interactions[cap.interaction_id]
    InteractionIn.model_validate(body)
    assert body["version"] == "v12"
    assert body["output"] == {"severity": 2, "route": "fast_track", "line": "auto"}
    assert body["subject_token"] == SUBJECT_TOKEN
    assert body["input"]["system"].startswith("<TEXT:tok_")


def test_wrapped_async_openai_call(mock: respx.MockRouter, ingest: FakeIngest, sdk: roadbed_sdk.Recorder, aoai: openai.AsyncOpenAI) -> None:
    _openai(mock, GATEWAY_HEADERS)
    client = roadbed_sdk.wrap_openai(aoai)

    async def go() -> str:
        resp = await client.chat.completions.create(model="claims-triage-prod", messages=MESSAGES)
        return resp.choices[0].message.content or ""

    assert asyncio.run(go()) == TRIAGE
    roadbed_sdk.flush()
    assert list(ingest.interactions) == ["ix_gateway_0001"]


def test_tool_call_output_is_captured(mock: respx.MockRouter, ingest: FakeIngest, sdk: roadbed_sdk.Recorder, oai: openai.OpenAI) -> None:
    payload = chat_completion("")
    payload["choices"][0]["message"] = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "triage", "arguments": TRIAGE}}]}
    mock.post(f"{GATEWAY}/chat/completions").mock(return_value=httpx.Response(200, json=payload))
    client = roadbed_sdk.wrap_openai(oai)
    client.chat.completions.create(model="m", messages=MESSAGES)
    roadbed_sdk.flush()
    (body,) = ingest.interactions.values()
    assert body["output"] == json.loads(TRIAGE)


def test_model_errors_reach_the_host_and_nothing_is_spooled(mock: respx.MockRouter, ingest: FakeIngest, sdk: roadbed_sdk.Recorder, oai: openai.OpenAI) -> None:
    mock.post(f"{GATEWAY}/chat/completions").mock(return_value=httpx.Response(400, json={"error": {"message": "bad"}}))
    client = roadbed_sdk.wrap_openai(oai)
    with pytest.raises(openai.BadRequestError):
        client.chat.completions.create(model="m", messages=MESSAGES)
    assert sdk.spool.counts()["interaction"] == 0


def test_capture_failure_never_raises_into_the_host(mock: respx.MockRouter, ingest: FakeIngest, sdk: roadbed_sdk.Recorder, oai: openai.OpenAI,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    _openai(mock)
    client = roadbed_sdk.wrap_openai(oai)

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(sdk.spool, "put", boom)
    resp = client.chat.completions.create(model="m", messages=MESSAGES)
    assert resp.choices[0].message.content == TRIAGE
    assert roadbed_sdk.verdict("ix_1", "approve") is None
    assert roadbed_sdk.outcome("ix_1", {"paid": True}) is None


def test_bad_verdict_is_refused_without_raising(sdk: roadbed_sdk.Recorder) -> None:
    assert roadbed_sdk.verdict("ix_1", "fix", {"severity": 3}) is None  # a fix needs reason_code
    assert roadbed_sdk.verdict("ix_1", "maybe") is None  # type: ignore[arg-type]
    assert sdk.spool.counts()["verdict"] == 0


def test_wrapping_twice_and_before_init_is_harmless(mock: respx.MockRouter, ingest: FakeIngest, oai: openai.OpenAI) -> None:
    _openai(mock)
    client = roadbed_sdk.wrap_openai(roadbed_sdk.wrap_openai(oai))
    resp = client.chat.completions.create(model="m", messages=MESSAGES)  # no init: no capture, no error
    assert resp.choices[0].message.content == TRIAGE
    assert ingest.posts == 0
    assert roadbed_sdk.flush() == {}
    assert API
    _ = TEST_KEY


def test_a_colliding_gateway_id_gets_a_fresh_id(mock: respx.MockRouter, ingest: FakeIngest, sdk: roadbed_sdk.Recorder,
                                                oai: openai.OpenAI) -> None:
    _openai(mock, {"x-roadbed-interaction-id": "ix_gw_same"})
    client = roadbed_sdk.wrap_openai(oai)
    ids = []
    for text in ("Claim 1: hail.", "Claim 2: flood."):
        with roadbed_sdk.capture() as cap:
            client.chat.completions.create(model="m", messages=[{"role": "user", "content": text}])
        ids.append(cap.interaction_id)
    assert ids[0] == "ix_gw_same" and ids[1] and ids[1] != "ix_gw_same"
    roadbed_sdk.flush()
    assert set(ingest.interactions) == set(ids)
