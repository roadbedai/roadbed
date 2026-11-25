"""HTTP client for the control plane (tests/cli_fixtures/openapi.yaml, v0.1).

No proxy or netrc from the environment, no redirects, bearer token from the token file only. Every failure is an
ApiError (exit 3) that carries the RFC 9457 problem fields the server sent: code, rule, audit_seq.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from roadbed import __version__
from roadbed.cli.config import Config

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


@dataclass
class ApiError(Exception):
    message: str
    status: int | None = None
    code: str | None = None
    rule: str | None = None
    audit_seq: int | None = None

    def __str__(self) -> str:
        head = " ".join(str(x) for x in (self.status, self.code, self.rule) if x)
        seq = f" (audit seq {self.audit_seq})" if self.audit_seq else ""
        return f"{head}{seq}: {self.message}" if head else self.message

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "code": self.code, "rule": self.rule, "audit_seq": self.audit_seq, "detail": self.message}


def seg(v: str | int) -> str:
    """One path segment, escaped (an alias or model name never reaches another route)."""
    return quote(str(v), safe="")


class Api:
    def __init__(self, cfg: Config, *, transport: httpx.BaseTransport | None = None) -> None:
        headers = {"accept": "application/json", "user-agent": f"roadbed-cli/{__version__}"}
        token = cfg.token()
        if token:
            headers["authorization"] = f"Bearer {token}"
        self.http = httpx.Client(base_url=cfg.require_url(), headers=headers, timeout=TIMEOUT, trust_env=False,
                                 follow_redirects=False, transport=transport)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> Api:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request(self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None,
                idempotency_key: str | None = None) -> httpx.Response:
        headers = {"idempotency-key": idempotency_key} if idempotency_key else None
        try:
            r = self.http.request(method, path, json=json, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise ApiError(f"{method} {path}: {type(e).__name__}") from None
        if r.status_code >= 300:
            raise problem(method, path, r)
        return r

    def get(self, path: str, **params: Any) -> Any:
        return _json(self.request("GET", path, params={k: v for k, v in params.items() if v is not None} or None))

    def post(self, path: str, body: Any, *, idempotency_key: str | None = None) -> tuple[Any, httpx.Response]:
        r = self.request("POST", path, json=body, idempotency_key=idempotency_key)
        return _json(r), r


def new_key() -> str:
    return str(uuid.uuid4())


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        raise ApiError(f"{r.request.method} {r.request.url.path}: response is not JSON", r.status_code) from None


def problem(method: str, path: str, r: httpx.Response) -> ApiError:
    if 300 <= r.status_code < 400:
        return ApiError(f"{method} {path}: redirect to {r.headers.get('location', '?')} not followed", r.status_code)
    try:
        body = r.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return ApiError(f"{method} {path}: HTTP {r.status_code}", r.status_code)
    detail = body.get("detail") or body.get("title") or f"HTTP {r.status_code}"
    if body.get("errors") and isinstance(body["errors"], list):
        locs = ["/".join(str(x) for x in e.get("loc", [])) + f" {e.get('msg', '')}".rstrip()
                for e in body["errors"] if isinstance(e, dict)]
        detail = f"{detail}: {'; '.join(locs)}" if locs else detail
    seq = body.get("audit_seq")
    return ApiError(str(detail), r.status_code, body.get("code"), body.get("rule"), seq if isinstance(seq, int) else None)
