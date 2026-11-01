"""roadbed.lock: every tracked file (path, sha256, size) and every bucket object the tree names (SPEC.md, Hashing 6-7)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from roadbed import SPEC_VERSION
from roadbed.asset.files import LOCK, AssetValidationError, read_doc, safe_rel, schema_for, tree_files, validate
from roadbed.asset.hashing import sha256_file
from roadbed.asset.objects import ObjectError, ObjectStore


@dataclass(frozen=True)
class NamedObject:
    oid: str
    ref: str  # the tracked path that names it
    size: int | None  # declared in the tree, when the naming file says


def named_objects(root: Path, files: list[str] | None = None) -> list[NamedObject]:
    """Every bucket object a tracked file names: pointers, bucket-only snapshot parts, adapters, MinHash objects and
    `oid:` receipt subjects. Files that do not parse are skipped here; the schema check reports them."""
    out: list[NamedObject] = []
    for rel in files if files is not None else tree_files(root):
        name = schema_for(rel)
        if name not in ("object-pointer", "snapshot-manifest", "seal", "model-version", "receipt"):
            continue
        try:
            doc = read_doc(root / rel)
        except Exception:  # noqa: BLE001  (reported by the schema check)
            continue
        if not isinstance(doc, dict):
            continue
        try:
            if name == "object-pointer":
                out.append(NamedObject(doc["oid"], rel, doc["size"]))
            elif name == "snapshot-manifest":
                out += [NamedObject(p["oid"], rel, p["size"]) for p in doc["parts"] if not p.get("path")]
            elif name == "seal" and doc.get("minhash_oid"):
                out.append(NamedObject(doc["minhash_oid"], rel, None))
            elif name == "model-version":
                out.append(NamedObject(doc["adapter"]["oid"], rel, doc["adapter"]["size"]))
            elif name == "receipt":
                out += [NamedObject(s["name"].removeprefix("oid:"), rel, None) for s in doc["subject"] if s["name"].startswith("oid:")]
        except (KeyError, TypeError, AttributeError):
            continue
    return out


class LockError(Exception):
    pass


def build_lock(root: str | Path, *, store: ObjectStore | None = None, previous: dict[str, Any] | None = None,
               synthetic: bool | None = None) -> dict[str, Any]:
    """The lock document for the tree as it is now. Object sizes come from the tree, else the store, else the
    previous lock. Write it last, after every other file (SPEC.md, Hashing rule 7)."""
    r = Path(root)
    files = [f for f in tree_files(r) if f != LOCK]
    prev = {o["oid"]: o["size"] for o in (previous or {}).get("objects", [])}
    objs: dict[str, dict[str, Any]] = {}
    for o in sorted(named_objects(r, files), key=lambda o: o.ref.encode()):
        if o.oid in objs:
            continue
        size = o.size
        if size is None and store is not None and store.has(o.oid):
            size = len(store.get(o.oid))
        if size is None:
            size = prev.get(o.oid)
        if size is None:
            raise LockError(f"size of object {o.oid} (named in {o.ref}) is unknown: pass a store or the previous lock")
        objs[o.oid] = {"oid": o.oid, "size": size, "ref": o.ref}
    doc: dict[str, Any] = {
        "spec": SPEC_VERSION,
        "files": [{"path": f, "sha256": sha256_file(r / f), "size": (r / f).stat().st_size} for f in files],
        "objects": sorted(objs.values(), key=lambda o: o["oid"]),
    }
    if synthetic is not None:
        doc["synthetic"] = synthetic
    return doc


def write_lock(root: str | Path, doc: dict[str, Any]) -> None:
    (Path(root) / LOCK).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@dataclass
class LockProblem:
    code: str
    detail: str


def check_lock(root: Path, *, store: ObjectStore | None = None) -> tuple[LockProblem | None, int, int]:
    """Compare roadbed.lock with the tree. Returns (first problem or None, files checked, objects checked)."""
    lf = root / LOCK
    if not lf.is_file():
        return LockProblem("V-LOCK-MISSING", f"{LOCK} not found"), 0, 0
    try:
        lock = json.loads(lf.read_text(encoding="utf-8"))
        validate(lock, "lock")
        paths = [f["path"] for f in lock["files"]]
        oids = [o["oid"] for o in lock["objects"]]
    except (ValueError, KeyError, TypeError, AssetValidationError) as e:
        return LockProblem("V-SCHEMA-INVALID", f"{LOCK}: {e}"), 0, 0
    bad = [p for p in paths if not isinstance(p, str) or not safe_rel(p)]
    if bad:
        return LockProblem("V-PATH-UNSAFE", f"{LOCK} lists a path outside the asset: {bad[0]!r}"), 0, 0
    if paths != sorted(set(paths), key=lambda s: s.encode("utf-8")) or oids != sorted(set(oids)):
        return LockProblem("V-LOCK-INCOMPLETE", f"{LOCK} is not sorted and unique"), 0, 0
    tree = [f for f in tree_files(root) if f != LOCK]
    unlisted = sorted(set(tree) - set(paths))
    if unlisted:
        return LockProblem("V-LOCK-INCOMPLETE", f"{LOCK} does not list {', '.join(unlisted[:5])}"), 0, 0
    links = [f for f in tree if (root / f).is_symlink()]
    if links:
        return LockProblem("V-PATH-UNSAFE", f"{links[0]} is a symlink; an asset holds plain files only"), 0, 0
    for f in lock["files"]:
        p = root / f["path"]
        if p.is_symlink() or not p.is_file():
            return LockProblem("V-HASH-MISSING", f"{f['path']} listed in {LOCK} but not found"), 0, 0
        if sha256_file(p) != f["sha256"] or p.stat().st_size != f["size"]:
            return LockProblem("V-HASH-MISMATCH", f"{f['path']} does not match {LOCK}"), 0, 0
    named = named_objects(root, tree)
    missing = sorted({o.oid for o in named} - set(oids))
    if missing:
        ref = next(o.ref for o in named if o.oid == missing[0])
        return LockProblem("V-LOCK-INCOMPLETE", f"{LOCK} does not list object {missing[0]} named in {ref}"), 0, 0
    extra = sorted(set(oids) - {o.oid for o in named})
    if extra:
        return LockProblem("V-LOCK-INCOMPLETE", f"{LOCK} lists object {extra[0]} that no tracked file names"), 0, 0
    declared = {o.oid: o.size for o in named if o.size is not None}
    checked = 0
    for o in lock["objects"]:
        if o["oid"] in declared and declared[o["oid"]] != o["size"]:
            return LockProblem("V-HASH-MISMATCH", f"object {o['oid']}: size {o['size']} in {LOCK}, {declared[o['oid']]} in the tree"), 0, 0
        if store is not None and store.has(o["oid"]):
            try:
                data = store.get(o["oid"])  # re-hashes the bytes
            except ObjectError as e:
                return LockProblem("V-HASH-MISMATCH", str(e)), 0, 0
            if len(data) != o["size"]:
                return LockProblem("V-HASH-MISMATCH", f"object {o['oid']} size does not match {LOCK}"), 0, 0
            checked += 1
    return None, len(paths), checked
