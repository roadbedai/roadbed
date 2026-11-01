"""Digests of SPEC.md "Hashing": file digests, JCS digests, row hashes, Merkle roots, object keys.

Every digest is lowercase hex without a prefix, except `input_ref` (`sha256:<hex>`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

import rfc8785

from roadbed.hashing import sha256_canonical, sha256_file

__all__ = [
    "ROW_HASH_FIELDS", "jcs", "jcs_sha256", "merkle_root", "object_key", "row_hash", "sha256_bytes", "sha256_file",
]

ROW_HASH_FIELDS = ("interaction_id", "input_ref", "output", "verdict", "reason_code", "outcome", "ts")
_EMPTY = hashlib.sha256(b"").digest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def jcs(obj: Any) -> bytes:
    """RFC 8785 canonical JSON bytes of obj."""
    return rfc8785.dumps(obj)


def jcs_sha256(obj: Any) -> str:
    """sha256(JCS(obj)). Hash the stored JSON as parsed, never a model re-dump (SPEC.md, Hashing rule 0)."""
    return sha256_canonical(obj)


def row_hash(row: Mapping[str, Any]) -> str:
    """sha256(JCS({interaction_id, input_ref, output, verdict, reason_code, outcome, ts})). A missing key is null."""
    return jcs_sha256({k: row.get(k) for k in ROW_HASH_FIELDS})


def _is_digest(h: str) -> bool:
    return len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def merkle_root(row_hashes: Iterable[str]) -> str:
    """RFC 6962 section 2.1 tree hash over the row hashes sorted ascending, duplicates kept.

    leaf = sha256(0x00 || 32 digest bytes); node = sha256(0x01 || left || right); split at the largest
    power of two below n; the empty set hashes to sha256(b"").
    """
    hs = sorted(row_hashes)
    for h in hs:
        if not _is_digest(h):
            raise ValueError(f"not a lowercase sha256 hex digest: {h!r}")
    level = [hashlib.sha256(b"\x00" + bytes.fromhex(h)).digest() for h in hs]
    if not level:
        return _EMPTY.hex()
    return _mth(level).hex()


def _mth(leaves: list[bytes]) -> bytes:
    # Recursion depth is log2(n), so a million leaves are fine.
    n = len(leaves)
    if n == 1:
        return leaves[0]
    k = 1
    while k * 2 < n:
        k *= 2
    return hashlib.sha256(b"\x01" + _mth(leaves[:k]) + _mth(leaves[k:])).digest()


def object_key(oid: str) -> str:
    """Bucket key of an object: objects/sha256/<oid[0:2]>/<oid[2:4]>/<oid>."""
    if not _is_digest(oid):
        raise ValueError(f"not an oid: {oid!r}")
    return f"objects/sha256/{oid[0:2]}/{oid[2:4]}/{oid}"
