"""The recorder: pseudonymise, validate, spool, and flush in a background thread. Never raises into the host app."""

from __future__ import annotations

import atexit
import base64
import contextvars
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import rfc8785
from pydantic import BaseModel, ValidationError

from roadbed.contracts import InteractionIn, OutcomeIn, VerdictIn

from ._pseudo import Pseudonymiser, Session, hmac_hex, hmac_token, normalise_pii
from ._sink import BATCH_MAX, ID_FIELD, Sink
from ._spool import KINDS, Kind, Spool

log = logging.getLogger("roadbed_sdk")

HDR_INTERACTION = "x-roadbed-interaction-id"
HDR_VERSION = "x-roadbed-model-version"


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_DIGIT_RUN = re.compile(r"\d{7}")


def new_id(prefix: str) -> str:
    """prefix_ plus 26 lowercase base32 characters (a-z, 2-7) of 16 random bytes.

    A run of 9 or more digits reads as a phone number to the engine guard, and the platform audit guard refuses a run
    of 7 or more. Hex ids hold such a run often, so the draw is repeated until the id has none.
    """
    while True:
        tail = base64.b32encode(os.urandom(16)).decode().lower().rstrip("=")
        if not _DIGIT_RUN.search(tail):
            return f"{prefix}_{tail}"


@dataclass
class Capture:
    """Per-call context set with `roadbed_sdk.capture(...)`. After a wrapped call, interaction_id holds its id."""

    interaction_id: str | None = None
    subject: str | None = None
    pii: Mapping[str, str] | Iterable[str] | None = None
    slices: Mapping[str, str] | None = None
    model: str | None = None
    synthetic: bool | None = None
    interaction_ids: list[str] = field(default_factory=list)

    def next_id(self) -> str:
        """The host's id for the first call, a fresh id for later calls (one id per interaction, never reused)."""
        if self.interaction_id and not self.interaction_ids:
            return self.interaction_id
        return new_id("ix")


_current: contextvars.ContextVar[Capture | None] = contextvars.ContextVar("roadbed_capture", default=None)
_last_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("roadbed_last_interaction_id", default=None)


@contextmanager
def capture(*, interaction_id: str | None = None, subject: str | None = None, pii: Mapping[str, str] | Iterable[str] | None = None,
            slices: Mapping[str, str] | None = None, model: str | None = None, synthetic: bool | None = None) -> Iterator[Capture]:
    """Context for the wrapped calls inside it.

    subject: the data subject's id in the host system; recorded only as an HMAC token (tok_...).
    pii: values the host knows are personal data in this call (names, claim numbers); they become placeholders.
    slices: slice labels such as {"line": "bodily_injury"}.
    """
    # A host may pass its own primary key (an int): use its text, so the id check and the header see a string.
    ix = str(interaction_id) if interaction_id is not None else None
    cap = Capture(interaction_id=ix, subject=subject, pii=pii, slices=slices, model=model, synthetic=synthetic)
    token = _current.set(cap)
    try:
        yield cap
    finally:
        _current.reset(token)


def current_capture() -> Capture | None:
    return _current.get()


def last_interaction_id() -> str | None:
    """Id of the last interaction captured in this thread or task."""
    return _last_id.get()


@dataclass
class Prepared:
    interaction_id: str
    cap: Capture | None
    t0: float


class Recorder:
    def __init__(self, *, sink: Sink, spool: Spool, pseudo: Pseudonymiser, profile_id: str, hmac_key_id: str,
                 model: str | None, synthetic: bool | None, background: bool, flush_interval: float) -> None:
        self.sink = sink
        self.spool = spool
        self.pseudo = pseudo
        self.profile_id = profile_id
        self.hmac_key_id = hmac_key_id
        self.model = model
        self.synthetic = synthetic
        self._warned_synthetic = False
        self.flush_interval = flush_interval
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._flush_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        if background:
            self._thread = threading.Thread(target=self._loop, name="roadbed-sdk-flush", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------ capture

    def prepare(self) -> Prepared:
        cap = _current.get()
        ix = cap.next_id() if cap else new_id("ix")
        if not self.pseudo.safe_id(ix):
            # Checked before it leaves: the id goes to the gateway as a request header, and the gateway logs it.
            log.warning("roadbed_sdk: interaction id refused (not a plain id); using a generated id")
            ix = new_id("ix")
        return Prepared(interaction_id=ix, cap=cap, t0=time.perf_counter())

    def record_interaction(self, prep: Prepared, *, request_model: str | None, input: dict[str, Any], output: dict[str, Any],
                           response_model: str | None, headers: Mapping[str, str] | None, latency_ms: float) -> str | None:
        """Pseudonymise and spool one interaction. Returns its id, or None when it could not be recorded."""
        try:
            cap = prep.cap
            hdr = {k.lower(): v for k, v in (headers or {}).items()}
            ix = hdr.get(HDR_INTERACTION) or prep.interaction_id
            if not self.pseudo.safe_id(ix):
                # An id with personal data in it would reach the spool, the server and the logs: use a fresh one.
                log.warning("roadbed_sdk: interaction id refused (not a plain id); using a generated id")
                ix = new_id("ix")
            version = hdr.get(HDR_VERSION)
            session = Session()
            known = normalise_pii(cap.pii if cap else None)
            p_input = self.pseudo.value(input, session, known)
            p_output = self.pseudo.value(output, session, known)
            if not isinstance(p_output, dict) or not isinstance(p_input, dict):
                raise TypeError("input and output must be objects")
            body: dict[str, Any] = {
                "interaction_id": ix,
                "model": self._model_str("model", (cap.model if cap and cap.model else None) or self.model or request_model, known),
                "alias": self._model_str("alias", request_model, known) if version and request_model else None,
                "version": self._model_str("version", version or response_model or request_model, known),
                "ts": utc_now(),
                # Content address of the pseudonymised input (SPEC: the stored input is the pseudonymised one).
                "input_ref": "sha256:" + hashlib.sha256(rfc8785.dumps(p_input)).hexdigest(),
                "input": p_input,
                "output": p_output,
                "profile_id": self.profile_id,
                "hmac_key_id": self.hmac_key_id,
                "subject_token": hmac_token(self.pseudo.key, cap.subject) if cap and cap.subject else None,
                "slices": self.pseudo.labels(cap.slices, session, known) if cap and cap.slices else None,
                "latency_ms": round(max(latency_ms, 0.0), 3),
                "synthetic": self._synthetic(cap),
            }
            body = {k: v for k, v in body.items() if v is not None}
            if not self._spool("interaction", InteractionIn, body):
                # An interaction id is never reused: a colliding id (from the gateway or the host) would drop this
                # interaction and bind later verdicts to the earlier one. Record it under a fresh id instead.
                log.warning("roadbed_sdk: interaction id %s already spooled; using a generated id", ix)
                ix = body["interaction_id"] = new_id("ix")
                self._spool("interaction", InteractionIn, body)
            if cap is not None:
                cap.interaction_ids.append(ix)
                cap.interaction_id = ix
            _last_id.set(ix)
            return ix
        except Exception as e:  # never raise into the host app
            safe = prep.interaction_id if self.pseudo.safe_id(prep.interaction_id) else "<refused id>"
            log.warning("roadbed_sdk: interaction %s not recorded: %s", safe, type(e).__name__)
            return None

    def record_verdict(self, interaction_id: str, action: str, verdict: Mapping[str, Any] | None, reason_code: str | None,
                       verdict_id: str | None, ts: str | None, pii: Any) -> str | None:
        vid = verdict_id or new_id("vd")
        try:
            self._check_ids(interaction_id, vid)
            body: dict[str, Any] = {"verdict_id": vid, "interaction_id": interaction_id, "action": action, "ts": ts or utc_now()}
            if verdict is not None:
                body["verdict"] = self.pseudo.value(dict(verdict), Session(), normalise_pii(pii))
            if reason_code is not None:
                body["reason_code"] = reason_code
            self._spool("verdict", VerdictIn, body)
            return vid
        except Exception as e:
            log.warning("roadbed_sdk: verdict %s not recorded: %s", vid if self.pseudo.safe_id(vid) else "<refused id>", type(e).__name__)
            return None

    def record_outcome(self, interaction_id: str, outcome: Mapping[str, Any], source: str, outcome_id: str | None,
                       observed_at: str | None, pii: Any) -> str | None:
        oid = outcome_id or new_id("oc")
        try:
            self._check_ids(interaction_id, oid)
            body = {
                "outcome_id": oid, "interaction_id": interaction_id,
                "outcome": self.pseudo.value(dict(outcome), Session(), normalise_pii(pii)),
                "source": source, "observed_at": observed_at or utc_now(),
            }
            self._spool("outcome", OutcomeIn, body)
            return oid
        except Exception as e:
            log.warning("roadbed_sdk: outcome %s not recorded: %s", oid if self.pseudo.safe_id(oid) else "<refused id>", type(e).__name__)
            return None

    def _model_str(self, field: str, value: str | None, known: list[tuple[str, str]]) -> str:
        """Model, alias and version from host kwargs or gateway headers: kept only if they hold no personal data."""
        if not value:
            return "unknown"
        if not self.pseudo.safe_model(value, known):
            log.warning("roadbed_sdk: %s string refused (not a plain name); using 'unknown'", field)
            return "unknown"
        return value

    def _synthetic(self, cap: Capture | None) -> bool | None:
        """Sticky: when init says synthetic, no capture can clear it."""
        if self.synthetic:
            return True
        if cap is not None and cap.synthetic:
            if not self._warned_synthetic:
                self._warned_synthetic = True
                log.warning("roadbed_sdk: synthetic=True in a capture, but not in init(); wrapped calls outside a capture "
                            "carry no synthetic flag. A synthetic tenant should pass synthetic=True to init().")
            return True
        return cap.synthetic if cap is not None and cap.synthetic is not None else self.synthetic

    def _check_ids(self, *ids: str) -> None:
        if not all(self.pseudo.safe_id(i) for i in ids):
            raise ValueError("ids must be plain ids without personal data")

    def keyed_id(self, prefix: str, obj: Any) -> str:
        """Deterministic id from content, keyed with the tenant key (a plain hash of raw values could be reversed)."""
        return f"{prefix}_" + hmac_hex(self.pseudo.key, prefix, rfc8785.dumps(obj).decode())[:32]

    def _spool(self, kind: Kind, model: type[BaseModel], body: dict[str, Any]) -> bool:
        """Validate and store. False when the id was already spooled (the first body is kept)."""
        try:
            model.model_validate(body)
        except ValidationError as e:
            # The pydantic message holds the values: log the field names only.
            fields = ",".join(".".join(str(p) for p in err["loc"]) for err in e.errors())
            raise ValueError(f"invalid {kind}: {fields}") from None
        # allow_nan=False: NaN or inf raises ValueError here (the caller reports "not recorded") instead of
        # spooling a row that the HTTP client cannot encode later.
        stored = self.spool.put(kind, body[ID_FIELD[kind]], json.dumps(body, separators=(",", ":"), sort_keys=True, allow_nan=False))
        if not stored:
            log.info("roadbed_sdk: %s %s already spooled; kept the first", kind, body[ID_FIELD[kind]])
        self._wake.set()
        return stored

    # ------------------------------------------------------------ flush

    def flush(self, *, force: bool = True) -> dict[str, int]:
        """Send what is due (everything when force). Interactions first, so verdicts and outcomes find them.
        Returns the spool counts afterwards. Never raises."""
        try:
            with self._flush_lock:
                for kind in KINDS:
                    self._flush_kind(kind, force)
            return self.spool.counts()
        except Exception as e:
            log.warning("roadbed_sdk: flush failed: %s", type(e).__name__)
            return {}

    def _flush_kind(self, kind: Kind, force: bool) -> None:
        seen: set[str] = set()
        while True:
            rows = [(i, b) for i, b in self.spool.due(kind, BATCH_MAX, force=force) if i not in seen]
            if not rows:
                return
            seen.update(i for i, _ in rows)
            items = [json.loads(b) for _, b in rows]
            try:
                res = self.sink.write(kind, items)
            except Exception as e:
                log.warning("roadbed_sdk: sink failed: %s", type(e).__name__)
                self.spool.retry(kind, [i for i, _ in rows])
                return
            for i in self.spool.first_attempts(kind, res.duplicates):
                # Nothing was sent before under this id, so the server holds an earlier body: an id was reused.
                log.warning("roadbed_sdk: %s %s was already on the server (id reused); the server kept the earlier one", kind, i)
            self.spool.ack(kind, res.accepted)
            self.spool.retry(kind, res.retry)
            for reason in set(res.rejected.values()):
                self.spool.reject(kind, [i for i, r in res.rejected.items() if r == reason], reason)
            if res.retry:
                return  # server or network trouble: back off, do not hammer

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.flush_interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            self.flush(force=False)

    def close(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
        if self._flush_lock.acquire(timeout=timeout):
            try:
                deadline = time.monotonic() + timeout
                for kind in KINDS:
                    if time.monotonic() < deadline:
                        try:
                            self._flush_kind(kind, force=True)
                        except Exception as e:
                            log.warning("roadbed_sdk: final flush failed: %s", type(e).__name__)
            finally:
                self._flush_lock.release()
        try:
            self.spool.close()
            close = getattr(self.sink, "close", None)
            if callable(close):
                close()
        except Exception as e:
            log.warning("roadbed_sdk: close failed: %s", type(e).__name__)


_state_lock = threading.Lock()
_state: Recorder | None = None


def get() -> Recorder | None:
    return _state


def set_state(rec: Recorder | None) -> None:
    global _state
    with _state_lock:
        old, _state = _state, rec
    if old is not None:
        old.close()


@atexit.register
def _shutdown() -> None:
    set_state(None)
