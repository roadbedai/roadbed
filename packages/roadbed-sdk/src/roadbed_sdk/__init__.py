"""Roadbed capture SDK.

    import roadbed_sdk
    roadbed_sdk.init(url, token, tenant_key, profile_id="insurer-default@v1")
    client = roadbed_sdk.wrap_openai(OpenAI(base_url=gateway))
    with roadbed_sdk.capture(subject="claimant-123", pii=["Jane Doe"], slices={"line": "auto"}) as cap:
        client.chat.completions.create(...)
    roadbed_sdk.verdict(cap.interaction_id, "fix", {"severity": 3}, reason_code="underestimated_injury")

Only init() raises (bad configuration). Capture, verdict, outcome, reconcile and flush never raise into the host app.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Mapping
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

from . import _runtime
from ._errors import ConfigError, ProfileRequiredError
from ._pseudo import MIN_KEY_BYTES, Pseudonymiser, Redactor, TextMode, derive_key_id, hmac_token, key_bytes
from ._reconcile import ReconcileReport, reconcile
from ._runtime import Capture, Recorder, capture, last_interaction_id
from ._sink import HttpSink, JsonlSink, Sink, WriteResult, check_url
from ._spool import Spool
from ._wrap import wrap_anthropic, wrap_openai

__all__ = [
    "Capture", "ConfigError", "HttpSink", "JsonlSink", "Spool", "ProfileRequiredError", "ReconcileReport", "Recorder", "Redactor", "Sink", "WriteResult",
    "capture", "flush", "init", "init_local", "last_interaction_id", "outcome", "reconcile", "shutdown", "subject_token", "verdict",
    "wrap_anthropic", "wrap_openai",
]

__version__ = version("roadbed-sdk")


def _default_spool(url: str) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "roadbed-sdk"
    return base / f"spool-{hashlib.sha256(url.encode()).hexdigest()[:12]}.sqlite3"


def _check(tenant_key: str | bytes, profile_id: str, text: TextMode, redactor: Redactor | None) -> tuple[bytes, Redactor | None]:
    if not profile_id or not str(profile_id).strip():
        raise ProfileRequiredError()
    key = key_bytes(tenant_key) if tenant_key else b""
    if len(key) < MIN_KEY_BYTES:
        raise ConfigError(f"roadbed_sdk: tenant_key must be at least {MIN_KEY_BYTES} bytes")
    if text not in ("tokenize", "redact"):
        raise ConfigError("roadbed_sdk: text must be 'tokenize' or 'redact'")
    if text == "redact" and redactor is None:
        from ._redact import PresidioRedactor

        redactor = PresidioRedactor()
    return key, redactor


def _start(sink: Sink, spool: Spool, key: bytes, profile_id: str, model: str | None, hmac_key_id: str | None, text: TextMode,
           redactor: Redactor | None, keep: Iterable[str], synthetic: bool | None, background: bool, flush_interval: float) -> Recorder:
    rec = Recorder(
        sink=sink, spool=spool, pseudo=Pseudonymiser(key, text=text, redactor=redactor, keep=keep),
        profile_id=str(profile_id).strip(), hmac_key_id=hmac_key_id or derive_key_id(key), model=model,
        synthetic=synthetic, background=background, flush_interval=flush_interval,
    )
    _runtime.set_state(rec)
    return rec


def _local_spool(events_dir: Path) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "roadbed-sdk"
    return base / f"spool-{hashlib.sha256(str(events_dir.resolve()).encode()).hexdigest()[:12]}.sqlite3"


def init(url: str, token: str, tenant_key: str | bytes, profile_id: str, *, model: str | None = None, hmac_key_id: str | None = None,
         text: TextMode = "tokenize", redactor: Redactor | None = None, keep: Iterable[str] = (), synthetic: bool | None = None,
         spool_path: str | Path | None = None, sink: Sink | None = None, background: bool = True, flush_interval: float = 1.0,
         allow_http: bool = False) -> Recorder:
    """Start capturing. Raises ConfigError (ProfileRequiredError without profile_id); nothing else raises later.

    tenant_key: the tenant's HMAC key, at least 16 bytes. It stays in this process; only its id is recorded.
    profile_id: the pseudonymisation profile, e.g. 'insurer-default@v1'. Required: the server refuses ingest without it.
    text: 'tokenize' (default) turns free text into HMAC tokens; 'redact' runs a named-entity redactor (Presidio extra).
    keep: output and input keys whose short identifier values are labels to keep as they are (e.g. 'route', 'severity').
    """
    key, redactor = _check(tenant_key, profile_id, text, redactor)
    if sink is None:
        sink = HttpSink(url, token, allow_http=allow_http)
    else:
        check_url(url, allow_http=allow_http)
    return _start(sink, Spool(spool_path or _default_spool(url)), key, profile_id, model, hmac_key_id, text, redactor, keep,
                  synthetic, background, flush_interval)


def init_local(events_dir: str | Path, tenant_key: str | bytes, profile_id: str, *, model: str | None = None,
               hmac_key_id: str | None = None, text: TextMode = "tokenize", redactor: Redactor | None = None,
               keep: Iterable[str] = (), synthetic: bool | None = None, spool_path: str | Path | None = None,
               background: bool = True, flush_interval: float = 1.0) -> Recorder:
    """Start capturing into local files, with no server: no url, no token, no network.

    Interactions, verdicts and outcomes are appended to events_dir/interactions.jsonl, verdicts.jsonl and outcomes.jsonl,
    which `roadbed evals build --events events_dir` reads. The other arguments are those of init().
    """
    key, redactor = _check(tenant_key, profile_id, text, redactor)
    events = Path(events_dir)
    return _start(JsonlSink(events), Spool(spool_path or _local_spool(events)), key, profile_id, model, hmac_key_id, text, redactor,
                  keep, synthetic, background, flush_interval)


def verdict(interaction_id: str, action: Literal["approve", "fix", "reject", "escalate"], verdict: Mapping[str, Any] | None = None,
            reason_code: str | None = None, *, verdict_id: str | None = None, ts: str | None = None,
            pii: Mapping[str, str] | Iterable[str] | None = None) -> str | None:
    """Spool a reviewer verdict. A fix needs verdict and reason_code. Returns the verdict id, or None (logged)."""
    rec = _runtime.get()
    if rec is None:
        return None
    return rec.record_verdict(interaction_id, action, verdict, reason_code, verdict_id, ts, pii)


def outcome(interaction_id: str, outcome: Mapping[str, Any], *, source: Literal["system_of_record", "csv"] = "system_of_record",
            outcome_id: str | None = None, observed_at: str | None = None,
            pii: Mapping[str, str] | Iterable[str] | None = None) -> str | None:
    """Spool a late system-of-record outcome. Returns the outcome id, or None (logged)."""
    rec = _runtime.get()
    if rec is None:
        return None
    return rec.record_outcome(interaction_id, outcome, source, outcome_id, observed_at, pii)


def subject_token(subject: str) -> str | None:
    """The tok_ HMAC token of a subject id, as captured. Use it for erasure requests (DELETE /v1/subjects/{token})."""
    rec = _runtime.get()
    if rec is None:
        return None
    try:
        return hmac_token(rec.pseudo.key, subject)
    except Exception as e:
        _runtime.log.warning("roadbed_sdk: subject_token not computed: %s", type(e).__name__)
        return None


def flush() -> dict[str, int]:
    """Send everything spooled now. Returns the spool counts afterwards. Never raises."""
    rec = _runtime.get()
    return rec.flush(force=True) if rec is not None else {}


def shutdown() -> None:
    """Final flush attempt, then close the spool. Called at exit as well."""
    _runtime.set_state(None)


