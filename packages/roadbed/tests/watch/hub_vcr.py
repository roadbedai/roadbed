"""vcrpy setup for Hub cassettes.

CI and default runs replay only (record_mode "none": an unrecorded request fails the test).
Re-record on a machine with network, from the repo root:

    rm packages/roadbed/tests/cassettes/hub_*.yaml
    ROADBED_VCR_RECORD=once uv run pytest packages/roadbed/tests/watch -q

Requests keep only the accept header. Responses keep only the headers below. No cookie,
token, request id or personal header is ever written.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import vcr

CASSETTES = Path(__file__).resolve().parents[1] / "cassettes"
KEEP_REQUEST_HEADERS = frozenset({"accept"})
KEEP_RESPONSE_HEADERS = frozenset({"content-type", "etag", "x-repo-commit"})


def scrub_request(request: Any) -> Any:
    for k in list(request.headers):
        if k.lower() not in KEEP_REQUEST_HEADERS:
            del request.headers[k]
    return request


def scrub_response(response: dict[str, Any]) -> dict[str, Any]:
    response["headers"] = {k: v for k, v in response["headers"].items() if k.lower() in KEEP_RESPONSE_HEADERS}
    return response


def hub_vcr() -> vcr.VCR:
    return vcr.VCR(
        cassette_library_dir=str(CASSETTES),
        record_mode=os.environ.get("ROADBED_VCR_RECORD", "none"),
        match_on=["method", "scheme", "host", "port", "path", "query"],
        before_record_request=scrub_request,
        before_record_response=scrub_response,
        decode_compressed_response=True,
    )


def cassette(name: str) -> Any:
    """Context manager replaying (or, when recording, writing) cassettes/<name>."""
    return hub_vcr().use_cassette(name, allow_playback_repeats=True)
