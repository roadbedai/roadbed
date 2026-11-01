"""SHA-256 helpers. Digests are lowercase hex without a prefix."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import rfc8785


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_canonical(obj: Any) -> str:
    """sha256 of the RFC 8785 (JCS) canonical JSON encoding of obj."""
    return hashlib.sha256(rfc8785.dumps(obj)).hexdigest()
