"""reconcile(csv): late outcomes from a system-of-record export, as OutcomeIn with source 'csv'."""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import _runtime

log = logging.getLogger("roadbed_sdk")


@dataclass
class ReconcileReport:
    rows: int = 0
    spooled: int = 0
    skipped: int = 0
    error: str | None = None


def _ts(v: str) -> str | None:
    v = v.strip()
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        if len(v) > 10:
            return None  # a time without a zone is ambiguous: skip rather than guess
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cell(v: str) -> Any:
    s = v.strip()
    if s == "":
        return None
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    for cast in (int, float):
        try:
            return cast(s)
        except ValueError:
            pass
    return s


def reconcile(path: str | Path, *, id_column: str = "interaction_id", observed_at_column: str = "observed_at",
              columns: Iterable[str] | None = None) -> ReconcileReport:
    """Spool one outcome per CSV row. The outcome holds the other columns (or `columns`), pseudonymised like any output.

    The outcome id is an HMAC of the row with the tenant key, so running the same file twice adds nothing.
    Rows without an interaction id or a valid observed_at (ISO date, or date-time with a zone) are skipped.
    Never raises; read the report.
    """
    rep = ReconcileReport()
    rec = _runtime.get()
    if rec is None:
        rep.error = "not_initialised"
        log.warning("roadbed_sdk: reconcile called before init")
        return rep
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            wanted = list(columns) if columns is not None else [c for c in (reader.fieldnames or []) if c not in (id_column, observed_at_column)]
            for row in reader:
                rep.rows += 1
                ix = (row.get(id_column) or "").strip()
                observed = _ts(row.get(observed_at_column) or "")
                if not ix or observed is None:
                    rep.skipped += 1
                    continue
                outcome = {c: _cell(row.get(c) or "") for c in wanted}
                oid = rec.keyed_id("oc", {"interaction_id": ix, "observed_at": observed, "outcome": outcome})
                if rec.record_outcome(ix, outcome, "csv", oid, observed, None):
                    rep.spooled += 1
                else:
                    rep.skipped += 1
    except (OSError, csv.Error, UnicodeDecodeError) as e:
        rep.error = type(e).__name__
        log.warning("roadbed_sdk: reconcile stopped: %s", type(e).__name__)
    return rep
