"""Capture overhead: p50 of (wrapped call - plain call) under 1 ms. Marked slow; run with `-m slow`.

Both clients hit the same respx route, so the transport cost cancels. The wrapped side includes pseudonymisation,
validation and the SQLite spool write; the flush runs in the background thread as in production.
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

import httpx
import openai
import pytest
import respx

import roadbed_sdk
from conftest import API, GATEWAY, GATEWAY_HEADERS, TEST_KEY, TRIAGE, FakeIngest, RespxBridge, chat_completion

N = 400
MESSAGES = [
    {"role": "system", "content": "Triage the claim. Answer JSON with severity, route and line."},
    {"role": "user", "content": "Claim by Jane Doe (jane.doe@example.com, +44 7700 900123): rear-ended at a junction, neck pain, car towed."},
]


@pytest.mark.slow
def test_capture_overhead_p50_under_1ms(mock: respx.MockRouter, ingest: FakeIngest, spool_path: Path) -> None:
    import httpx2

    seq = iter(range(10**6))
    mock.post(f"{GATEWAY}/chat/completions").mock(side_effect=lambda req: httpx.Response(
        200, json=chat_completion(TRIAGE), headers={**GATEWAY_HEADERS, "x-roadbed-interaction-id": f"ix_gw_{next(seq)}"}))
    plain = openai.OpenAI(base_url=GATEWAY, api_key="unused", max_retries=0, http_client=httpx2.Client(transport=RespxBridge(mock)))
    wrapped = roadbed_sdk.wrap_openai(openai.OpenAI(base_url=GATEWAY, api_key="unused", max_retries=0,
                                                    http_client=httpx2.Client(transport=RespxBridge(mock))))
    roadbed_sdk.init(API, "test-token", TEST_KEY, "insurer-default@v1", keep=("route", "line", "severity"), spool_path=spool_path)

    def timed(client: openai.OpenAI) -> float:
        t0 = time.perf_counter()
        client.chat.completions.create(model="claims-triage-prod", messages=MESSAGES)
        return time.perf_counter() - t0

    for _ in range(50):  # warm up both paths
        timed(plain), timed(wrapped)
    diffs = []
    for _ in range(N):  # interleave so drift hits both sides
        p, w = timed(plain), timed(wrapped)
        diffs.append((w - p) * 1000.0)
    p50 = statistics.median(diffs)
    print(f"\ncapture overhead over {N} paired calls: p50 {p50:.3f} ms, p90 {statistics.quantiles(diffs, n=10)[-1]:.3f} ms")
    assert p50 < 1.0
    roadbed_sdk.flush()
    assert len(ingest.interactions) == N + 50  # every wrapped call was captured
