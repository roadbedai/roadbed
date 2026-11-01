"""`roadbed evidence verify DIR`: offline check of one evidence pack, rebases/N/evidence (SPEC.md, Manifests, Sign-off).

Checks, in order: the evidence MANIFEST.sha256 parses; MANIFEST.sig is a valid ed25519 signature over it with
policy/keys/evidence.pub (and a trusted key id, when given); every listed file has its hash; the manifest lists every
evidence file and rebases/N/MANIFEST.sha256; every file that rebase manifest lists has its hash, so the signature
covers the whole rebase; approvals.json matches its schema and names this rebase.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import pydantic

from roadbed.asset import Check, SignatureError, sha256_file, verify_signature
from roadbed.asset.files import parse_manifest, safe_rel
from roadbed.cli.asset import find_root
from roadbed.contracts import Approvals, json_schema

EVIDENCE = "evidence"
MANIFEST = "MANIFEST.sha256"
SIG = "MANIFEST.sig"
EVIDENCE_KEY = "policy/keys/evidence.pub"


class EvidenceFailed(Exception):
    def __init__(self, check: Check) -> None:
        super().__init__(f"{check.code}: {check.detail}")
        self.check = check


@dataclass
class Pack:
    root: Path
    rebase: int
    evidence: Path

    @property
    def rrel(self) -> str:
        return f"rebases/{self.rebase}"


def locate(arg: Path, asset: Path | None) -> Pack:
    """DIR is rebases/N/evidence or rebases/N. The asset root is --asset or found by searching up."""
    d = arg.resolve()
    if d.name != EVIDENCE:
        d = d / EVIDENCE
    if not d.is_dir():
        raise FileNotFoundError(f"evidence directory not found: {arg}")
    rdir = d.parent
    if not re.fullmatch(r"\d+", rdir.name) or rdir.parent.name != "rebases":
        raise ValueError(f"evidence must be in rebases/<n>/evidence (got {arg})")
    root = asset.resolve() if asset else find_root(rdir)
    if rdir.parent.parent.resolve() != root:
        raise ValueError(f"{arg} is not inside the asset {root}")
    return Pack(root, int(rdir.name), d)


def _entries(pack: Pack, rel: str, code_missing: str) -> list[dict[str, str]] | Check:
    p = pack.root / rel
    try:
        entries = parse_manifest(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Check(code_missing, "", False, f"{rel} not found")
    except (ValueError, UnicodeDecodeError) as e:
        return Check("V-MANIFEST-MALFORMED", "", False, f"{rel}: {e}")
    for ent in entries:
        if not safe_rel(ent["path"]):
            return Check("V-MANIFEST-MALFORMED", "", False, f"{rel}: path outside the asset: {ent['path']!r}")
    return entries


def _hashes(pack: Pack, rel: str, entries: list[dict[str, str]]) -> Check | None:
    for e in entries:
        f = pack.root / e["path"]
        if f.is_symlink() or not f.is_file():
            return Check("V-HASH-MISSING", "", False, f"{e['path']} listed in {rel} but not found")
        if sha256_file(f) != e["sha256"]:
            return Check("V-HASH-MISMATCH", "", False, f"{e['path']} does not match {rel}")
    return None


def run(pack: Pack, on_check: Callable[[Check], None], *, trusted_keyids: Collection[str] | None = None) -> dict[str, Any]:
    """Run the checks in order. Returns approvals.json. Raises EvidenceFailed on the first failure."""

    def report(c: Check) -> None:
        on_check(c)
        if c.ok is False:
            raise EvidenceFailed(c)

    mrel = f"{pack.rrel}/{EVIDENCE}/{MANIFEST}"
    got = _entries(pack, mrel, "V-MANIFEST-MISSING")
    if isinstance(got, Check):
        report(got)
        raise AssertionError("unreachable")
    entries = got

    try:
        kid = verify_signature(pack.root, f"{pack.rrel}/{EVIDENCE}/{SIG}", signed=mrel, key_path=EVIDENCE_KEY, payload="raw",
                               trusted_keyids=trusted_keyids)
    except SignatureError as e:
        report(Check(e.code, "", False, str(e)))
    except (OSError, ValueError) as e:
        report(Check("V-SIG-INVALID", "", False, f"{SIG}: {e}"))
    pinned = "pinned" if trusted_keyids is not None else "not pinned (pass --trust-key or set trust_keys)"
    report(Check("V-SIG-INVALID", "manifest signature OK", True, f"key {kid[:12]}, {pinned}"))

    if (bad := _hashes(pack, mrel, entries)) is not None:
        report(bad)
    report(Check("V-HASH-MISMATCH", "evidence hashes OK", True, f"{len(entries)} files"))

    listed = {e["path"] for e in entries}
    on_disk = {p.relative_to(pack.root).as_posix() for p in pack.evidence.rglob("*") if p.is_file() or p.is_symlink()}
    need = (on_disk - {mrel, f"{pack.rrel}/{EVIDENCE}/{SIG}"}) | {f"{pack.rrel}/{MANIFEST}"}
    missing = sorted(need - listed)
    if missing:
        report(Check("V-MANIFEST-INCOMPLETE", "", False, f"{mrel} does not list {', '.join(missing)}"))
    report(Check("V-MANIFEST-INCOMPLETE", "evidence manifest complete", True, f"covers {pack.rrel}/{MANIFEST}"))

    rmrel = f"{pack.rrel}/{MANIFEST}"
    got = _entries(pack, rmrel, "V-MANIFEST-MISSING")
    if isinstance(got, Check):
        report(got)
        raise AssertionError("unreachable")
    if (bad := _hashes(pack, rmrel, got)) is not None:
        report(bad)
    report(Check("V-HASH-MISMATCH", "rebase hashes OK", True, f"{len(got)} files under the signature"))

    arel = f"{pack.rrel}/{EVIDENCE}/approvals.json"
    try:
        approvals = json.loads((pack.root / arel).read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(json_schema("approvals", reader=True)).validate(approvals)
        Approvals.model_validate(approvals)
    except FileNotFoundError:
        report(Check("V-SCHEMA-INVALID", "", False, f"{arel} not found"))
    except (ValueError, jsonschema.ValidationError, pydantic.ValidationError) as e:
        msg = e.message if isinstance(e, jsonschema.ValidationError) else str(e).splitlines()[0]
        report(Check("V-SCHEMA-INVALID", "", False, f"{arel}: {msg}"))
    if approvals.get("rebase") != pack.rebase:
        report(Check("V-SCHEMA-INVALID", "", False, f"{arel} is for rebase {approvals.get('rebase')}, not {pack.rebase}"))
    report(Check("V-SCHEMA-INVALID", "approvals OK", True, f"pack version {approvals['pack_version']}, {approvals['status_text']}"))
    return approvals
