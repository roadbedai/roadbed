"""Where spooled bodies go. HttpSink talks to the control plane (contracts/openapi.yaml, tag ingest)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from roadbed.contracts import InteractionIn, OutcomeIn, VerdictIn

from ._errors import ConfigError
from ._spool import Kind

log = logging.getLogger("roadbed_sdk")

BATCH_MAX = 500  # POST /v1/interactions:batch maxItems
MODELS: dict[Kind, type[BaseModel]] = {"interaction": InteractionIn, "verdict": VerdictIn, "outcome": OutcomeIn}
ID_FIELD: dict[Kind, str] = {"interaction": "interaction_id", "verdict": "verdict_id", "outcome": "outcome_id"}
_PATH: dict[Kind, str] = {"interaction": "/v1/interactions", "verdict": "/v1/verdicts", "outcome": "/v1/outcomes"}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass
class WriteResult:
    """Per-id outcome of one write. accepted: stored or duplicate (leave the spool). retry: keep and resend later.
    rejected: refused for good (kept in the spool, out of the send loop), with a reason code.
    duplicates: the accepted ids the server already had."""

    accepted: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    retry: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class Sink(Protocol):
    """Delivers request bodies (InteractionIn, VerdictIn, OutcomeIn as JSON dicts). Must not raise; the flusher
    treats an exception as `retry` for every item."""

    def write(self, kind: Kind, items: Sequence[dict[str, Any]]) -> WriteResult: ...


def refuse_invalid(kind: Kind, items: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Client-side check against roadbed.contracts before anything leaves the process.
    An interaction without profile_id is refused here (the server would answer 422)."""
    ok: list[dict[str, Any]] = []
    bad: dict[str, str] = {}
    for item in items:
        item_id = str(item.get(ID_FIELD[kind], ""))
        if kind == "interaction" and not item.get("profile_id"):
            # Never log the body: only the id and the reason code.
            log.error("roadbed_sdk: refused interaction %s: profile_id missing; ingest without a pseudonymisation profile is not allowed", item_id)
            bad[item_id] = "profile_required"
            continue
        try:
            MODELS[kind].model_validate(item)
        except ValidationError as e:
            fields = ",".join(".".join(str(p) for p in err["loc"]) for err in e.errors())
            log.error("roadbed_sdk: refused %s %s: invalid fields %s", kind, item_id, fields)
            bad[item_id] = "invalid_body"
            continue
        ok.append(item)
    return ok, bad


def check_url(url: str, *, allow_http: bool) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ConfigError(f"roadbed_sdk: url must be http(s)://host[:port], got scheme {parts.scheme!r}")
    if parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS and not allow_http:
        raise ConfigError("roadbed_sdk: refusing to send the API token over plain http to a non-local host. Use https, or allow_http=True on a private link.")
    return url.rstrip("/")


class HttpSink:
    def __init__(self, url: str, token: str, *, timeout: float = 10.0, allow_http: bool = False, client: httpx.Client | None = None) -> None:
        if not token:
            raise ConfigError("roadbed_sdk: token is required")
        self._client = client or httpx.Client(
            base_url=check_url(url, allow_http=allow_http),
            headers={"Authorization": f"Bearer {token}", "User-Agent": "roadbed-sdk/0.0.1"},
            timeout=timeout,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def write(self, kind: Kind, items: Sequence[dict[str, Any]]) -> WriteResult:
        res = WriteResult()
        items, res.rejected = refuse_invalid(kind, items)
        if not items:
            return res
        if kind == "interaction":
            for i in range(0, len(items), BATCH_MAX):
                self._batch(items[i:i + BATCH_MAX], res)
        else:
            for item in items:
                self._one(kind, item, res)
        return res

    def _post(self, path: str, body: dict[str, Any]) -> httpx.Response | None:
        """None on a network error. A body JSON cannot encode (NaN, inf) raises ValueError before anything is sent."""
        try:
            return self._client.post(path, json=body)
        except httpx.HTTPError as e:
            log.warning("roadbed_sdk: POST %s failed: %s", path, type(e).__name__)
            return None

    def _batch(self, items: list[dict[str, Any]], res: WriteResult) -> None:
        ids = [str(it["interaction_id"]) for it in items]
        try:
            r = self._post("/v1/interactions:batch", {"items": items})
        except ValueError:
            # One body cannot be encoded: send one by one, so only that item is refused and the rest flow.
            for it in items:
                self._one("interaction", it, res)
            return
        if r is not None and r.status_code == 202:
            try:
                acks = r.json()["items"]
                acked = {str(a["interaction_id"]) for a in acks}
                dups = {str(a["interaction_id"]) for a in acks if a.get("duplicate") is True}
            except (ValueError, KeyError, TypeError, AttributeError):
                acked, dups = set(), set()
            res.accepted += [i for i in ids if i in acked]
            res.duplicates += [i for i in ids if i in dups]
            res.retry += [i for i in ids if i not in acked]
            return
        if r is not None and r.status_code == 422:
            # The whole batch is refused when one item is invalid: send one by one to find it.
            for it in items:
                self._one("interaction", it, res)
            return
        self._log_status("/v1/interactions:batch", r)
        res.retry += ids

    def _one(self, kind: Kind, item: dict[str, Any], res: WriteResult) -> None:
        item_id = str(item[ID_FIELD[kind]])
        try:
            r = self._post(_PATH[kind], item)
        except ValueError:
            res.rejected[item_id] = "not_json"
            log.error("roadbed_sdk: refused %s %s: not_json (a NaN or infinite number)", kind, item_id)
            return
        if r is not None and r.status_code == 202:
            res.accepted.append(item_id)
            try:
                if r.json().get("duplicate") is True:
                    res.duplicates.append(item_id)
            except (ValueError, AttributeError):
                pass
        elif r is not None and r.status_code == 422:
            res.rejected[item_id] = "http_422"
            log.error("roadbed_sdk: server refused %s %s (422)", kind, item_id)
        else:
            # 404 for a verdict or outcome: its interaction is not ingested yet. 401, 403, 409, 429, 5xx, network: later.
            self._log_status(_PATH[kind], r)
            res.retry.append(item_id)

    @staticmethod
    def _log_status(path: str, r: httpx.Response | None) -> None:
        if r is not None:
            log.warning("roadbed_sdk: POST %s answered %s; keeping the items in the spool", path, r.status_code)
