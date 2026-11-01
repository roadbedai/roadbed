"""Content-addressed objects: a local directory laid out like the bucket (SPEC.md, Hashing rule 6).

`ObjectStore(dir)` reads and writes `<dir>/objects/sha256/<oid[0:2]>/<oid[2:4]>/<oid>`. A read re-hashes the bytes,
so a store can never hand back bytes under the wrong oid. The S3 client lives elsewhere; verify needs only reads.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from roadbed.asset.hashing import object_key, sha256_bytes


class ObjectError(Exception):
    """An object is missing or its bytes do not match its oid."""


class ObjectStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path(self, oid: str) -> Path:
        return self.root / object_key(oid)

    def has(self, oid: str) -> bool:
        return self.path(oid).is_file()

    def get(self, oid: str) -> bytes:
        p = self.path(oid)
        try:
            data = p.read_bytes()
        except FileNotFoundError:
            raise ObjectError(f"object {oid} not found in {self.root}") from None
        if sha256_bytes(data) != oid:
            raise ObjectError(f"object {oid} in {self.root} does not match its oid")
        return data

    def put(self, data: bytes) -> str:
        oid = sha256_bytes(data)
        p = self.path(oid)
        if p.is_file() and sha256_bytes(p.read_bytes()) == oid:
            return oid
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return oid
