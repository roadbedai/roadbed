"""Shared fixtures. The control plane and the model gateway are respx routes (no network).

FakeIngest stands in for the control plane ingest routes of contracts/openapi.yaml: it validates every body with
roadbed.contracts, stores by id and answers `duplicate: true` on a repeat. Tests assert what the SDK sends and keeps,
never a claim about the fake itself.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import httpx
import httpx2
import openai
import pytest
import respx

import roadbed_sdk
from roadbed.contracts import InteractionIn, OutcomeIn, VerdictIn

API = "https://api.roadbed.test"
GATEWAY = "https://gw.roadbed.test/v1"
# A test key, not a secret. Its HMAC vectors in test_pseudo.py come from openssl, not from this code.
TEST_KEY = b"roadbed-test-key-0123456789abcdef"


@dataclass
class FakeIngest:
    interactions: dict[str, dict[str, Any]] = field(default_factory=dict)
    verdicts: dict[str, dict[str, Any]] = field(default_factory=dict)
    outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)
    posts: int = 0
    down: bool = False  # connection refused
    lose_acks: int = 0  # store the batch, then drop the connection before the answer (n times)
    raw_bodies: list[bytes] = field(default_factory=list)

    def _guard(self, request: httpx.Request) -> None:
        self.posts += 1
        self.raw_bodies.append(request.content)
        if self.down:
            raise httpx.ConnectError("control plane down", request=request)
        assert request.headers["authorization"] == "Bearer test-token"

    def batch(self, request: httpx.Request) -> httpx.Response:
        self._guard(request)
        body = json.loads(request.content)
        assert 1 <= len(body["items"]) <= 500
        acks = []
        for item in body["items"]:
            InteractionIn.model_validate(item)
            dup = item["interaction_id"] in self.interactions
            self.interactions.setdefault(item["interaction_id"], item)
            acks.append({"interaction_id": item["interaction_id"], "duplicate": dup})
        if self.lose_acks:
            self.lose_acks -= 1
            raise httpx.ReadError("connection reset after the server stored the batch", request=request)
        return httpx.Response(202, json={"items": acks})

    def one(self, request: httpx.Request) -> httpx.Response:
        self._guard(request)
        item = json.loads(request.content)
        InteractionIn.model_validate(item)
        dup = item["interaction_id"] in self.interactions
        self.interactions.setdefault(item["interaction_id"], item)
        return httpx.Response(202, json={"interaction_id": item["interaction_id"], "duplicate": dup})

    def verdict(self, request: httpx.Request) -> httpx.Response:
        self._guard(request)
        item = json.loads(request.content)
        VerdictIn.model_validate(item)
        if item["interaction_id"] not in self.interactions:
            return httpx.Response(404, json={"type": "about:blank", "title": "Not found", "status": 404, "code": "not_found"})
        dup = item["verdict_id"] in self.verdicts
        self.verdicts.setdefault(item["verdict_id"], item)
        return httpx.Response(202, json={"verdict_id": item["verdict_id"], "interaction_id": item["interaction_id"], "duplicate": dup})

    def outcome(self, request: httpx.Request) -> httpx.Response:
        self._guard(request)
        item = json.loads(request.content)
        OutcomeIn.model_validate(item)
        if item["interaction_id"] not in self.interactions:
            return httpx.Response(404, json={"type": "about:blank", "title": "Not found", "status": 404, "code": "not_found"})
        dup = item["outcome_id"] in self.outcomes
        self.outcomes.setdefault(item["outcome_id"], item)
        return httpx.Response(202, json={"outcome_id": item["outcome_id"], "interaction_id": item["interaction_id"], "duplicate": dup})


def chat_completion(content: str, model: str = "claims-triage-v12") -> dict[str, Any]:
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1762000000, "model": model,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def anthropic_message(content: str, model: str = "claims-triage-v12") -> dict[str, Any]:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": content}], "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


GATEWAY_HEADERS = {"x-roadbed-interaction-id": "ix_gateway_0001", "x-roadbed-model-version": "v12"}
TRIAGE = '{"severity": 2, "route": "fast_track", "line": "auto"}'


class RespxBridge(httpx2.BaseTransport, httpx2.AsyncBaseTransport):
    """openai>=3 and anthropic>=2 send through httpx2, which respx does not patch. This transport hands each httpx2
    request to the respx router (so routes, side effects and call records work) and converts the answer back."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.router = router

    @staticmethod
    def _req(request: httpx2.Request, content: bytes) -> httpx.Request:
        return httpx.Request(request.method, str(request.url), headers=list(request.headers.raw), content=content)

    @staticmethod
    def _resp(resp: httpx.Response, request: httpx2.Request) -> httpx2.Response:
        headers = [(k, v) for k, v in resp.headers.raw if k.lower() not in (b"content-encoding", b"transfer-encoding")]
        return httpx2.Response(resp.status_code, headers=headers, content=resp.read(), request=request)

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        return self._resp(self.router.handler(self._req(request, request.read())), request)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return self._resp(self.router.handler(self._req(request, await request.aread())), request)


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def oai(mock: respx.MockRouter) -> openai.OpenAI:
    return openai.OpenAI(base_url=GATEWAY, api_key="unused", max_retries=0, http_client=httpx2.Client(transport=RespxBridge(mock)))


@pytest.fixture
def aoai(mock: respx.MockRouter) -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(base_url=GATEWAY, api_key="unused", max_retries=0, http_client=httpx2.AsyncClient(transport=RespxBridge(mock)))


@pytest.fixture
def anth(mock: respx.MockRouter) -> anthropic.Anthropic:
    return anthropic.Anthropic(base_url=GATEWAY, api_key="unused", max_retries=0, http_client=httpx2.Client(transport=RespxBridge(mock)))


@pytest.fixture
def ingest(mock: respx.MockRouter) -> FakeIngest:
    fake = FakeIngest()
    mock.post(f"{API}/v1/interactions:batch").mock(side_effect=fake.batch)
    mock.post(f"{API}/v1/interactions").mock(side_effect=fake.one)
    mock.post(f"{API}/v1/verdicts").mock(side_effect=fake.verdict)
    mock.post(f"{API}/v1/outcomes").mock(side_effect=fake.outcome)
    return fake


@pytest.fixture
def spool_path(tmp_path: Path) -> Path:
    return tmp_path / "spool" / "spool.sqlite3"


@pytest.fixture
def sdk(ingest: FakeIngest, spool_path: Path) -> Iterator[roadbed_sdk.Recorder]:
    rec = roadbed_sdk.init(API, "test-token", TEST_KEY, "insurer-default@v1", model="claims-triage", keep=("route", "line", "severity"),
                           synthetic=True, spool_path=spool_path, background=False)
    yield rec
    roadbed_sdk.shutdown()


def spool_bytes(path: Path) -> bytes:
    """Every byte the spool wrote: the database, its WAL and its shared-memory file."""
    return b"".join(p.read_bytes() for p in path.parent.glob(path.name + "*"))


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    """A test that fails before shutdown must not leave its recorder (and its spool) to the next test."""
    yield
    roadbed_sdk.shutdown()
