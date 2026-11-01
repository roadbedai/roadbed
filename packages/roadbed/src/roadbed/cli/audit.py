"""`roadbed audit verify --export F`: offline check of an audit chain export (SPEC.md, Audit chain).

Built here against the audit-event and audit-anchor schemas, not against the control plane code:
each line is one AuditEvent; seq runs 1, 2, 3, ... without gaps; prev_hash is the hash of seq - 1 (64 zeros for
seq 1); hash = sha256(JCS(event without "hash")). An anchor (audit/anchors/<date>.json) pins head_seq to head_hash;
prev_anchor = sha256(JCS(previous anchor)).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import pydantic

from roadbed.asset import Check
from roadbed.asset.hashing import jcs_sha256
from roadbed.contracts import AuditAnchor, AuditEvent, json_schema

ZERO = "0" * 64


class AuditFailed(Exception):
    def __init__(self, check: Check) -> None:
        super().__init__(f"{check.code}: {check.detail}")
        self.check = check


@dataclass
class ChainResult:
    events: int
    first_seq: int
    head_seq: int
    head_hash: str
    denied: int
    synthetic_anchor: bool
    anchored: int = 0  # anchors whose head is in the export and matches


def _schema_error(doc: Any, name: str) -> str | None:
    try:
        jsonschema.Draft202012Validator(json_schema(name, reader=True)).validate(doc)
    except jsonschema.ValidationError as e:
        where = "/".join(str(p) for p in e.absolute_path)
        return f"{where}: {e.message}" if where else e.message
    return None


def read_export(path: Path) -> list[tuple[int, dict[str, Any]]]:
    """(line number, event) for every non-empty line. Raises AuditFailed on a line that is not a JSON object."""
    out = []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise AuditFailed(Check("V-AUDIT-SCHEMA", "", False, f"{path.name}: not UTF-8 ({e.reason})")) from None
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except ValueError as e:
            raise AuditFailed(Check("V-AUDIT-SCHEMA", "", False, f"line {lineno}: not JSON ({e})")) from None
        if not isinstance(doc, dict):
            raise AuditFailed(Check("V-AUDIT-SCHEMA", "", False, f"line {lineno}: not a JSON object"))
        out.append((lineno, doc))
    return out


def event_hash(doc: dict[str, Any]) -> str:
    return jcs_sha256({k: v for k, v in doc.items() if k != "hash"})


def check_chain(lines: list[tuple[int, dict[str, Any]]]) -> ChainResult:
    """Walk the chain in file order. Raises AuditFailed naming the first bad seq and the check that caught it."""
    if not lines:
        raise AuditFailed(Check("V-AUDIT-SCHEMA", "", False, "export holds no events"))
    prev_seq: int | None = None
    prev_hash: str | None = None
    denied = 0
    for lineno, doc in lines:
        where = f"line {lineno}" + (f" (seq {doc['seq']})" if isinstance(doc.get("seq"), int) else "")
        err = _schema_error(doc, "audit-event")
        if err is None:
            try:
                AuditEvent.model_validate(doc)
            except pydantic.ValidationError as e:
                err = str(e.errors()[0]["msg"])
        if err is not None:
            raise AuditFailed(Check("V-AUDIT-SCHEMA", "", False, f"{where}: {err}"))
        seq = doc["seq"]
        if prev_seq is not None and seq != prev_seq + 1:
            raise AuditFailed(Check("V-AUDIT-SEQ", "", False, f"{where}: expected seq {prev_seq + 1}, found {seq}"))
        if doc["hash"] != event_hash(doc):
            raise AuditFailed(Check("V-AUDIT-HASH", "", False, f"{where}: hash does not match sha256(JCS(event))"))
        want = prev_hash if prev_hash is not None else (ZERO if seq == 1 else None)
        if want is not None and doc["prev_hash"] != want:
            raise AuditFailed(Check("V-AUDIT-LINK", "", False, f"{where}: prev_hash is not the hash of seq {seq - 1}"))
        denied += doc["outcome"] == "denied"
        prev_seq, prev_hash = seq, doc["hash"]
    first = lines[0][1]["seq"]
    assert prev_seq is not None and prev_hash is not None
    return ChainResult(len(lines), first, prev_seq, prev_hash, denied, False)


def load_anchor(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise AuditFailed(Check("V-AUDIT-ANCHOR", "", False, f"{path.name}: {e}")) from None
    err = _schema_error(doc, "audit-anchor") if isinstance(doc, dict) else "not a JSON object"
    if err is None:
        try:
            AuditAnchor.model_validate(doc)
        except pydantic.ValidationError as e:
            err = str(e.errors()[0]["msg"])
    if err is not None:
        raise AuditFailed(Check("V-SCHEMA-INVALID", "", False, f"{path.name}: {err}"))
    return doc


def run(export: Path, anchors: list[Path], on_check: Callable[[Check], None], *, strict: bool = False) -> ChainResult:
    """Run the checks in order, report each, raise AuditFailed on the first failure."""

    def report(c: Check) -> None:
        on_check(c)
        if c.ok is False:
            raise AuditFailed(c)

    try:
        lines = read_export(export)
        res = check_chain(lines)
    except AuditFailed as e:
        on_check(e.check)
        raise
    except OSError as e:
        c = Check("V-AUDIT-SCHEMA", "", False, f"{export}: {e.strerror}")
        on_check(c)
        raise AuditFailed(c) from None
    span = f"{res.events} events, seq {res.first_seq}..{res.head_seq}, head {res.head_hash[:12]}"
    report(Check("V-AUDIT-HASH", "chain OK", True, span))
    if res.first_seq != 1:
        why = f"export starts at seq {res.first_seq}: the link to seq {res.first_seq - 1} is not checked"
        report(Check("V-AUDIT-LINK", "chain start: skipped", False if strict else None, why))

    by_seq = {doc["seq"]: doc for _, doc in lines}
    docs = sorted(((load_anchor(p), p) for p in anchors), key=lambda x: (x[0]["date"], x[0]["head_seq"]))
    if not docs:
        report(Check("V-AUDIT-ANCHOR", "head anchored: skipped (no --anchor)", False if strict else None, "pass --anchor audit/anchors/<date>.json"))
    prev: dict[str, Any] | None = None
    for doc, p in docs:
        res.synthetic_anchor |= bool(doc.get("synthetic"))
        if prev is not None and doc.get("prev_anchor") != jcs_sha256(prev):
            report(Check("V-AUDIT-ANCHOR", "", False, f"{p.name}: prev_anchor is not sha256(JCS) of the {prev['date']} anchor"))
        prev = doc
        ev = by_seq.get(doc["head_seq"])
        if ev is None:
            why = f"{p.name}: head seq {doc['head_seq']} is not in the export (seq {res.first_seq}..{res.head_seq})"
            report(Check("V-AUDIT-ANCHOR", f"anchor {doc['date']}: skipped", False if strict else None, why))
            continue
        if ev["hash"] != doc["head_hash"]:
            report(Check("V-AUDIT-ANCHOR", "", False, f"{p.name}: event seq {doc['head_seq']} hash differs from the anchored head_hash"))
        res.anchored += 1
        report(Check("V-AUDIT-ANCHOR", f"head anchored: {doc['date']} seq {doc['head_seq']:,}", True, doc["head_hash"][:12]))
    return res
