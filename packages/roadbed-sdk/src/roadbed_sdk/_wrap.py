"""Wrap OpenAI and Anthropic clients (sync and async) in place.

The wrapper calls the client's own raw-response method, bound before the patch, so it can read the gateway headers
`x-roadbed-interaction-id` and `x-roadbed-model-version`. It sends the interaction id it chose as a request header.
Exceptions from the model call itself reach the host unchanged; capture errors never do.
Streaming calls (stream=True) pass through without capture (carry-over).
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from . import _runtime
from ._runtime import HDR_INTERACTION, Prepared

log = logging.getLogger("roadbed_sdk")
Extract = Callable[[dict[str, Any], Any], tuple[dict[str, Any], dict[str, Any]]]


def _dump(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_none=True)
    if isinstance(obj, dict):
        return {k: _dump(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_dump(v) for v in obj]
    return obj


def _parse_output(text: str | None) -> dict[str, Any]:
    if text:
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            return obj
    return {"text": text or ""}


def _openai_extract(kwargs: dict[str, Any], resp: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    inp = {"messages": _dump(list(kwargs.get("messages") or []))}
    msg = resp.choices[0].message if getattr(resp, "choices", None) else None
    calls = getattr(msg, "tool_calls", None) if msg is not None else None
    if calls:
        fn = calls[0].function
        out = _parse_output(fn.arguments)
        out = out if "text" not in out else {"tool": fn.name, **out}
    else:
        out = _parse_output(getattr(msg, "content", None))
    return inp, out


def _anthropic_extract(kwargs: dict[str, Any], resp: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    inp: dict[str, Any] = {"messages": _dump(list(kwargs.get("messages") or []))}
    if kwargs.get("system") is not None:
        inp["system"] = _dump(kwargs["system"])
    blocks = list(getattr(resp, "content", None) or [])
    for b in blocks:
        if getattr(b, "type", None) == "tool_use" and isinstance(getattr(b, "input", None), dict):
            return inp, dict(b.input)
    text = "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", None) == "text")
    return inp, _parse_output(text)


def _with_header(kwargs: dict[str, Any], interaction_id: str) -> dict[str, Any]:
    headers = dict(kwargs.get("extra_headers") or {})
    headers.setdefault(HDR_INTERACTION, interaction_id)
    return {**kwargs, "extra_headers": headers}


def _record(rec: _runtime.Recorder, prep: Prepared, kwargs: dict[str, Any], raw: Any, resp: Any, extract: Extract) -> None:
    try:
        latency_ms = (time.perf_counter() - prep.t0) * 1000.0
        inp, out = extract(kwargs, resp)
        rec.record_interaction(prep, request_model=kwargs.get("model"), input=inp, output=out,
                               response_model=getattr(resp, "model", None), headers=dict(raw.headers), latency_ms=latency_ms)
    except Exception as e:
        log.warning("roadbed_sdk: capture failed: %s", type(e).__name__)


def _patch(resource: Any, extract: Extract) -> None:
    orig = resource.create
    if getattr(orig, "__roadbed_wrapped__", False):
        return
    raw_create = resource.with_raw_response.create  # cached_property: bound to the original create

    # The SDK decorators hide the coroutine function; the resource class name does not (AsyncCompletions, AsyncMessages).
    if inspect.iscoroutinefunction(inspect.unwrap(orig)) or type(resource).__name__.startswith("Async"):
        @functools.wraps(orig)
        async def acreate(*args: Any, **kwargs: Any) -> Any:
            rec = _runtime.get()
            if rec is None or kwargs.get("stream"):
                return await orig(*args, **kwargs)
            try:
                prep = rec.prepare()
                kw = _with_header(kwargs, prep.interaction_id)
            except Exception:
                return await orig(*args, **kwargs)
            raw = await raw_create(*args, **kw)
            resp = await raw.parse() if inspect.iscoroutinefunction(raw.parse) else raw.parse()
            _record(rec, prep, kwargs, raw, resp, extract)
            return resp

        acreate.__roadbed_wrapped__ = True  # type: ignore[attr-defined]
        resource.create = acreate
        return

    @functools.wraps(orig)
    def create(*args: Any, **kwargs: Any) -> Any:
        rec = _runtime.get()
        if rec is None or kwargs.get("stream"):
            return orig(*args, **kwargs)
        try:
            prep = rec.prepare()
            kw = _with_header(kwargs, prep.interaction_id)
        except Exception:
            return orig(*args, **kwargs)
        raw = raw_create(*args, **kw)
        resp = raw.parse()
        _record(rec, prep, kwargs, raw, resp, extract)
        return resp

    create.__roadbed_wrapped__ = True  # type: ignore[attr-defined]
    resource.create = create


def wrap_openai[C](client: C) -> C:
    """Capture every `client.chat.completions.create` call. Returns the same client. Never raises."""
    try:
        _patch(client.chat.completions, _openai_extract)  # type: ignore[attr-defined]
    except Exception as e:
        log.warning("roadbed_sdk: wrap_openai failed, client left unwrapped: %s", type(e).__name__)
    return client


def wrap_anthropic[C](client: C) -> C:
    """Capture every `client.messages.create` call. Returns the same client. Never raises."""
    try:
        _patch(client.messages, _anthropic_extract)  # type: ignore[attr-defined]
    except Exception as e:
        log.warning("roadbed_sdk: wrap_anthropic failed, client left unwrapped: %s", type(e).__name__)
    return client
