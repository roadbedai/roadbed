"""`roadbed verify rebases/N`: re-hash, re-validate, check signatures, the receipt chain, the seal order and
disjointness, and recompute scores.json, offline (SPEC.md, Verify and Status W02).

A rebase is **full** when it has a receipt, a receipt signature or an evidence/ directory, when its manifest names
one of them, when the asset has a roadbed.lock, policy keys, a version receipt or a full seal, or when the git
history ever held its receipt or a lock. Stripping files does not turn a full rebase into a Day 0 one. A Day 0 rebase
gets the four Day 0 checks and prints the same bytes as on Day 0.

Data verify cannot see (bucket objects, git history outside a clone) is a skipped check, never a pass. With
`strict=True` every skip fails and every file must meet the closed (producer) schema.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema
from pydantic import ValidationError as PydanticError

from roadbed.asset import gitlog
from roadbed.asset.day0 import Check, VerifyFailed, _hashes, _schemas, _scores, _seal
from roadbed.asset.files import (
    LOCK,
    MANIFEST,
    SYNTHETIC_MD,
    AssetValidationError,
    parse_manifest,
    read_doc,
    safe_rel,
    schema_for,
    tree_files,
    validate,
    validate_file,
)
from roadbed.asset.hashing import jcs_sha256, merkle_root, row_hash, sha256_bytes, sha256_file
from roadbed.asset.lock import check_lock, named_objects
from roadbed.asset.objects import ObjectError, ObjectStore
from roadbed.asset.overlap import (
    DEFAULT_NUM_PERM,
    DEFAULT_SHINGLE,
    DEFAULT_THRESHOLD,
    Overlap,
    exact_overlap,
    minhash,
    near_exact,
    near_minhash,
    parse_minhash_object,
    shingles,
)
from roadbed.asset.seal import holdout_row_hash
from roadbed.asset.signing import SignatureError, verify_signature
from roadbed.cli.asset import PRED_FILES, Asset, open_asset
from roadbed.contracts import SCHEMA_MODELS

__all__ = ["Check", "VerifyFailed", "VerifyResult", "is_full", "run", "verify"]

RECEIPT, RECEIPT_SIG, EVIDENCE = "receipt.json", "receipt.sig", "evidence"
RECEIPT_KEY, EVIDENCE_KEY = "policy/keys/receipt.pub", "policy/keys/evidence.pub"
# Files every full rebase has (SPEC.md, The asset tree). The rest of rebases/N/ must be listed when present.
FULL_REBASE_FILES = (RECEIPT, RECEIPT_SIG, "rebase.yaml", "scores.json", "gate.json", "contamination.json", *PRED_FILES.values())
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
FILE_EXT = re.compile(r"\.(ya?ml|json|jsonl|md|pdf|sha256|sig|pub|ptr|txt|csv|parquet|safetensors)$")


@dataclass
class VerifyResult:
    rebase: int
    full: bool
    checks: list[Check]
    failed: Check | None

    @property
    def ok(self) -> bool:
        return self.failed is None


# ---------------------------------------------------------------- mode


def is_full(root: Path, d: Path) -> bool:
    """True when the rebase is not Day 0 (see the module docstring)."""
    rrel = d.resolve().relative_to(root.resolve()).as_posix()
    if any((d / f).exists() for f in (RECEIPT, RECEIPT_SIG, EVIDENCE)):
        return True
    m = d / MANIFEST
    if m.is_file():
        text = m.read_text(encoding="utf-8", errors="replace")
        if f"{rrel}/{RECEIPT}" in text or f"{rrel}/{EVIDENCE}/" in text:
            return True
    if (root / LOCK).exists() or (root / "policy/keys").exists() or any(root.glob(f"versions/*/{RECEIPT}")):
        return True
    for seal in root.glob("data/holdout/*/SEAL.json"):
        try:
            if "merkle_root" in json.loads(seal.read_text(encoding="utf-8")):
                return True
        except ValueError:
            return True  # an unreadable seal is not a Day 0 seal; the full checks report it
    return gitlog.is_repo_root(root) and gitlog.ever_tracked(root, f"{rrel}/{RECEIPT}", f"{rrel}/{EVIDENCE}", LOCK)


# ---------------------------------------------------------------- context


@dataclass
class _Ctx:
    asset: Asset
    d: Path
    n: int
    store: ObjectStore | None
    strict: bool
    trusted: frozenset[str] | None
    root: Path = field(init=False)
    rrel: str = field(init=False)
    git: bool = field(init=False)
    files: list[str] = field(init=False)
    receipt: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None
    seal: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None
    seal_rel: str = ""
    holdout_rel: str = ""
    snapshot_rel: str = ""
    recipe_rel: str = ""
    train_rows: list[dict[str, Any]] | None = None
    holdout_rows: list[dict[str, Any]] | None = None
    legs: dict[str, Any] = field(default_factory=dict)
    overlap: Overlap | None = None

    def __post_init__(self) -> None:
        self.root = self.asset.root
        self.rrel = self.asset.rel(self.d)
        self.git = gitlog.is_repo_root(self.root)
        self.files = tree_files(self.root)
        self.receipt = _try_doc(self.d / RECEIPT)
        self.meta = _try_doc(self.d / "rebase.yaml")
        pred = (self.receipt or {}).get("predicate") or {}
        cfg = self.asset.cfg
        holdout_dir = str(cfg["data"]["holdout"]["path"]).rstrip("/")
        self.holdout_rel = f"{holdout_dir}/holdout.jsonl"
        self.seal_rel = f"{holdout_dir}/SEAL.json"
        snap_id = (pred.get("snapshot") or {}).get("id") or cfg["data"].get("snapshot") or ""
        verdicts = str(cfg["data"].get("verdicts") or "data/snapshots").rstrip("/")
        self.snapshot_rel = f"{verdicts}/{snap_id.removeprefix('S-')}/MANIFEST.json" if snap_id.startswith("S-") else ""
        self.recipe_rel = (pred.get("recipe") or {}).get("path") or cfg["recipe"]
        # Paths named by files are untrusted: anything that leaves the asset is reported by the manifest check.
        self.seal = _try_doc(self.root / self.seal_rel) if safe_rel(self.seal_rel) else None
        self.snapshot = _try_doc(self.root / self.snapshot_rel) if safe_rel(self.snapshot_rel) else None

    def skip_or_fail(self, code: str, label: str, why: str) -> Check:
        if self.strict:
            return Check(code, label, False, f"{why} (--strict)")
        return Check(code, f"{label}: skipped", None, why)


def _try_doc(p: Path) -> dict[str, Any] | None:
    try:
        doc = read_doc(p)
    except Exception:  # noqa: BLE001  (the schema check reports unreadable files)
        return None
    return doc if isinstance(doc, dict) else None


def _required(c: _Ctx) -> list[str]:
    """Every path the rebase MANIFEST must list (SPEC.md, Manifests), for a full rebase."""
    own = {f"{c.rrel}/{f}" for f in FULL_REBASE_FILES}
    for f in c.files:
        if f.startswith(c.rrel + "/") and f != f"{c.rrel}/{MANIFEST}" and not f.startswith(f"{c.rrel}/{EVIDENCE}/"):
            own.add(f)
    bound = {c.holdout_rel, c.seal_rel, c.recipe_rel}
    if c.snapshot_rel:
        bound.add(c.snapshot_rel)
    else:
        bound.add("<training snapshot MANIFEST.json: no snapshot id in the receipt or roadbed.yaml>")
    return sorted(own | bound)


# ---------------------------------------------------------------- full checks


def _lock(c: _Ctx) -> Check:
    problem, nfiles, nobj = check_lock(c.root, store=c.store)
    if problem:
        return Check(problem.code, "lock OK", False, problem.detail)
    return Check("V-LOCK-INCOMPLETE", "lock OK", True, f"{nfiles} files, {nobj} objects re-hashed")


def _all_schemas(c: _Ctx) -> Check:
    count = 0
    for rel in c.files:
        try:
            if validate_file(c.root, rel, strict=c.strict):
                count += 1
        except AssetValidationError as e:
            return Check("V-SCHEMA-INVALID", "every file matches its schema", False, str(e))
    form = "closed" if c.strict else "reader"
    return Check("V-SCHEMA-INVALID", "every file matches its schema", True, f"{count} files, {form} form")


def _docs(c: _Ctx) -> list[tuple[str, str, Any]]:
    out = []
    for rel in c.files:
        name = schema_for(rel)
        if name is None:
            continue
        doc = read_doc(c.root / rel)
        for row in doc if isinstance(doc, list) else [doc]:
            out.append((rel, name, row))
    return out


def _synthetic(c: _Ctx) -> Check:
    label = "synthetic flags OK"
    root_flag = c.asset.cfg.get("synthetic") is True
    count = 0
    for rel, name, doc in _docs(c):
        if not isinstance(doc, dict):
            continue
        flags = []
        if "synthetic" in SCHEMA_MODELS[name].model_fields:
            flags.append(("synthetic", doc.get("synthetic")))
        if name == "receipt":
            pred = doc.get("predicate") or {}
            flags.append(("predicate.synthetic", pred.get("synthetic")))
            if (pred.get("executor") or {}).get("name") in ("mock", "replay") and pred.get("synthetic") is not True:
                return Check("V-SYNTHETIC-FLAG", label, False, f"{rel}: a {pred['executor']['name']} receipt says synthetic: true")
        for key, val in flags:
            count += 1
            if root_flag and val is not True:
                return Check("V-SYNTHETIC-FLAG", label, False, f"{rel}: {key} is {json.dumps(val)} in a synthetic asset")
            if not root_flag and val is True:
                return Check("V-SYNTHETIC-FLAG", label, False, f"{rel} says synthetic but roadbed.yaml does not")
    if root_flag:
        for rel in c.files:
            if not rel.endswith(".jsonl"):
                continue
            parts = rel.split("/")[:-1]
            dirs = ["/".join(parts[:i]) for i in range(len(parts), 0, -1)] or [""]
            if not any((c.root / x / SYNTHETIC_MD).is_file() for x in dirs):
                return Check("V-SYNTHETIC-FLAG", label, False, f"no {SYNTHETIC_MD} next to {rel}")
        if not (c.root / SYNTHETIC_MD).is_file():
            return Check("V-SYNTHETIC-FLAG", label, False, f"no {SYNTHETIC_MD} at the asset root")
    return Check("V-SYNTHETIC-FLAG", label, True, f"{count} flags{', SYNTHETIC' if root_flag else ''}")


def _strings(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [s for k, x in v.items() for s in (_strings(k) + _strings(x))]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    return []


def _pii(c: _Ctx) -> Check:
    """No email address in any manifest, row, receipt, signature, audit, lineage or evidence file (SPEC.md, Personal
    data). Pseudonymised data carries tok_<hex> tokens; an address means raw personal data got through."""
    label = "no personal data (email) in the asset"
    texts: list[tuple[str, list[str]]] = [(rel, _strings(doc)) for rel, _, doc in _docs(c)]
    for rel in c.files:
        if rel.endswith((".md", ".sha256")):
            texts.append((rel, [(c.root / rel).read_text(encoding="utf-8", errors="replace")]))
    for rel, strings in texts:
        for s in strings:
            for m in EMAIL.finditer(s):
                if not FILE_EXT.search(m.group(0)):
                    return Check("V-PII", label, False, f"{rel} holds an email address")
    return Check("V-PII", label, True, f"{len({rel for rel, _ in texts})} files scanned")


def _signatures(c: _Ctx) -> Check:
    label = "signatures OK"
    jobs = [(rel[: -len(RECEIPT)] + RECEIPT_SIG, rel, RECEIPT_KEY, "jcs") for rel in c.files if schema_for(rel) == "receipt"]
    jobs += [(rel[: -len(MANIFEST)] + "MANIFEST.sig", rel, EVIDENCE_KEY, "raw")
             for rel in c.files if rel.startswith("rebases/") and rel.endswith(f"/{EVIDENCE}/{MANIFEST}")]
    if (c.d / EVIDENCE).is_dir() and f"{c.rrel}/{EVIDENCE}/{MANIFEST}" not in c.files:
        return Check("V-SIG-MISSING", label, False, f"{c.rrel}/{EVIDENCE}/{MANIFEST} not found")
    keys: set[str] = set()
    for sig, signed, key, payload in jobs:
        try:
            keys.add(verify_signature(c.root, sig, signed=signed, key_path=key, payload=payload,  # type: ignore[arg-type]
                                      trusted_keyids=c.trusted))
        except SignatureError as e:
            return Check(e.code, label, False, str(e))
        except (OSError, ValueError) as e:
            return Check("V-SIG-INVALID", label, False, f"{sig}: {e}")
    pinned = "pinned" if c.trusted is not None else "not pinned (pass --trust-key)"
    return Check("V-SIG-INVALID", label, True, f"{len(jobs)} files, keys {', '.join(sorted(k[:12] for k in keys))}; {pinned}")


def _evidence(c: _Ctx) -> Check:
    label = "evidence manifest OK"
    e = c.d / EVIDENCE
    if not e.is_dir():
        return Check("V-MANIFEST-MISSING", f"{label}: skipped", None, "no evidence/ yet")
    mrel = f"{c.rrel}/{EVIDENCE}/{MANIFEST}"
    try:
        entries = parse_manifest((c.root / mrel).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Check("V-MANIFEST-MISSING", label, False, f"{mrel} not found")
    except ValueError as err:
        return Check("V-MANIFEST-MALFORMED", label, False, f"{mrel}: {err}")
    listed = set()
    for ent in entries:
        rel = ent["path"]
        if not safe_rel(rel):
            return Check("V-MANIFEST-MALFORMED", label, False, f"{mrel}: path outside the asset: {rel!r}")
        f = c.root / rel
        if f.is_symlink() or not f.is_file():
            return Check("V-HASH-MISSING", label, False, f"{rel} listed in {mrel} but not found")
        if sha256_file(f) != ent["sha256"]:
            return Check("V-HASH-MISMATCH", label, False, f"{rel} does not match {mrel}")
        listed.add(rel)
    need = {f for f in c.files if f.startswith(f"{c.rrel}/{EVIDENCE}/")} - {mrel, f"{c.rrel}/{EVIDENCE}/MANIFEST.sig"}
    need.add(f"{c.rrel}/{MANIFEST}")
    missing = sorted(need - listed)
    if missing:
        return Check("V-MANIFEST-INCOMPLETE", label, False, f"{mrel} does not list {', '.join(missing)}")
    return Check("V-HASH-MISMATCH", label, True, f"{len(listed)} files")


def _chain(c: _Ctx) -> Check:
    label = "receipt chain OK"
    receipts = []
    for rel in c.files:
        if schema_for(rel) == "receipt":
            doc = read_doc(c.root / rel)
            p = doc["predicate"]
            receipts.append((p["started_at"], p["ended_at"], rel, doc))
    receipts.sort(key=lambda r: (r[0], r[1], r[2]))
    starts = [r[0] for r in receipts]
    if len(set(starts)) != len(starts):
        return Check("V-RECEIPT-CHAIN", label, False, "two receipts share started_at; the chain order is ambiguous")
    prev: str | None = None
    for _, _, rel, doc in receipts:
        got = doc["predicate"].get("prev_receipt")
        if got != prev:
            want = "none" if prev is None else prev[:12]
            return Check("V-RECEIPT-CHAIN", label, False, f"{rel}: prev_receipt is {got and got[:12]}, expected {want}")
        prev = jcs_sha256(doc)
    return Check("V-RECEIPT-CHAIN", label, True, f"{len(receipts)} receipts")


def _binding(c: _Ctx) -> Check:
    label = "receipt binds its inputs"
    rec, meta, seal, snap = c.receipt, c.meta or {}, c.seal, c.snapshot
    if rec is None:
        return Check("V-HASH-MISSING", label, False, f"{c.rrel}/{RECEIPT} not found or unreadable")
    p = rec["predicate"]
    if seal is None:
        return Check("V-HASH-MISSING", label, False, f"{c.seal_rel} not found or unreadable")
    if snap is None:
        return Check("V-HASH-MISSING", label, False, f"training snapshot {c.snapshot_rel or '(none named)'} not found or unreadable")

    def bad(msg: str) -> Check:
        return Check("V-RECEIPT-MISMATCH", label, False, f"{c.rrel}/{RECEIPT}: {msg}")

    if p.get("rebase") != c.n:
        return bad(f"rebase is {p.get('rebase')}, the directory is {c.n}")
    if p["model"] != c.asset.cfg["model"]:
        return bad(f"model {p['model']} is not {c.asset.cfg['model']}")
    if p["job"]["kind"] not in ("rebase", "train"):
        return bad(f"job kind {p['job']['kind']} does not train")
    if meta:
        mb = meta.get("base") or {}
        if (mb.get("repo"), mb.get("revision")) != (p["base"]["repo"], p["base"]["revision"]):
            return bad("base differs from rebase.yaml")
        for k, v in (("holdout", p["holdout"]["id"]), ("snapshot", p["snapshot"]["id"]), ("recipe", p["recipe"]["path"])):
            if meta.get(k) is not None and meta[k] != v:
                return bad(f"{k} {v} differs from rebase.yaml ({meta[k]})")
    names = {}
    for s in rec["subject"]:
        name, digest = s["name"], s["digest"]["sha256"]
        if name.startswith("oid:"):
            oid = name.removeprefix("oid:")
            if oid != digest:
                return bad(f"subject {name} has digest {digest[:12]}")
            if c.store is not None and c.store.has(oid):
                try:
                    c.store.get(oid)
                except ObjectError as e:
                    return Check("V-HASH-MISMATCH", label, False, str(e))
            continue
        if not safe_rel(name):
            return bad(f"subject path outside the asset: {name!r}")
        f = c.root / name
        if f.is_symlink() or not f.is_file():
            return Check("V-HASH-MISSING", label, False, f"{name} named by {c.rrel}/{RECEIPT} not found")
        if sha256_file(f) != digest:
            return Check("V-HASH-MISMATCH", label, False, f"{name} does not match {c.rrel}/{RECEIPT}")
        names[name] = digest
    for f in PRED_FILES.values():
        if f"{c.rrel}/{f}" not in names:
            return bad(f"does not name {c.rrel}/{f} as a subject")
    rp = p["recipe"]["path"]
    if not safe_rel(rp) or not (c.root / rp).is_file():
        return Check("V-HASH-MISSING", label, False, f"recipe {rp} named by the receipt not found")
    if sha256_file(c.root / rp) != p["recipe"]["sha256"]:
        return Check("V-HASH-MISMATCH", label, False, f"{rp} does not match the receipt recipe.sha256")
    if snap["id"] != p["snapshot"]["id"] or snap["merkle_root"] != p["snapshot"]["merkle_root"]:
        return bad(f"snapshot {p['snapshot']['id']} does not match {c.snapshot_rel}")
    if p["snapshot"].get("rows") is not None and p["snapshot"]["rows"] != snap["rows"]:
        return bad("snapshot rows differ from the snapshot manifest")
    h = p["holdout"]
    if h["id"] != c.asset.cfg["data"]["holdout"]["id"] or h["id"] != seal["id"]:
        return bad(f"holdout {h['id']} is not the asset's holdout {seal['id']}")
    if h["merkle_root"] != seal.get("merkle_root") or (h.get("rows") is not None and h["rows"] != seal["rows"]):
        return bad(f"holdout Merkle root or rows differ from {c.seal_rel}")
    if h["seal_seq"] != seal["audit_seq"] or h["sealed_at"] != seal["sealed_at"]:
        return Check("V-SEAL-ORDER", label, False, f"receipt holdout seal_seq/sealed_at differ from {c.seal_rel}")
    return Check("V-RECEIPT-MISMATCH", label, True, f"{len(rec['subject'])} subjects, recipe, snapshot, holdout")


CONFIG_FILES = ("roadbed_yaml_sha256", "slices_sha256", "scorers_sha256", "gate_policy_sha256", "seal_sha256")


def _config(c: _Ctx) -> Check:
    """predicate.config binds the files the job ran against (SPEC.md, Receipt, Config). Each field present is checked.
    The five config files must match in the working tree, because verify and the gate read them there, and in the
    commit that added the receipt when git history has it. lock_sha256 is the lock that commit holds; without that
    history the working tree lock is checked when it does not list the receipt (it is then still the starting lock),
    else the lock binding is a skipped check."""
    label = "receipt binds its config"
    cfg = ((c.receipt or {}).get("predicate") or {}).get("config")
    if not cfg:
        return Check("V-HASH-MISMATCH", label, True, "no config in the receipt (compatibility)")
    a = c.asset.cfg
    paths = dict(zip(CONFIG_FILES, ("roadbed.yaml", a["evals"]["slices"], a["evals"]["scorers"], a["gate"], c.seal_rel),
                     strict=True))
    receipt_rel = f"{c.rrel}/{RECEIPT}"
    commit: str | None = None
    if c.git:
        try:
            commit = gitlog.added_in(c.root, receipt_rel)
        except gitlog.GitError:
            commit = None  # not committed, or changed after: the seal order check reports that
    where = f"the commit that added {receipt_rel} ({commit[:12]})" if commit else "the working tree"
    for key, rel in paths.items():
        want = cfg.get(key)
        if want is None:
            continue
        f = c.root / rel
        if not safe_rel(rel) or f.is_symlink() or not f.is_file():
            return Check("V-HASH-MISSING", label, False, f"{rel} bound by {receipt_rel} config.{key} not found")
        if sha256_file(f) != want:
            return Check("V-HASH-MISMATCH", label, False, f"{rel} does not match {receipt_rel} config.{key}")
        if commit is not None:
            old = gitlog.blob(c.root, commit, rel)
            if old is None or sha256_bytes(old) != want:
                return Check("V-HASH-MISMATCH", label, False, f"{rel} in {where} does not match {receipt_rel} config.{key}")
    checked = [k for k in CONFIG_FILES if cfg.get(k) is not None]
    want = cfg.get("lock_sha256")
    if want is None:
        return Check("V-HASH-MISMATCH", label, True, f"{len(checked)} files")
    if commit is not None:
        lock = gitlog.blob(c.root, commit, LOCK)
    else:
        lock = (c.root / LOCK).read_bytes() if (c.root / LOCK).is_file() else None
        if lock is not None and receipt_rel in {e.get("path") for e in json.loads(lock).get("files", [])}:
            return c.skip_or_fail("V-HASH-MISMATCH", label, f"{len(checked)} files match; config.lock_sha256 needs the "
                                  f"lock from the commit that added {receipt_rel} (the working tree lock lists it)")
    if lock is None:
        return Check("V-HASH-MISSING", label, False, f"{LOCK} in {where} not found")
    if sha256_bytes(lock) != want:
        return Check("V-HASH-MISMATCH", label, False, f"{LOCK} in {where} does not match {receipt_rel} config.lock_sha256")
    return Check("V-HASH-MISMATCH", label, True, f"{len(checked)} files and {LOCK} from {where}")


def _policy(c: _Ctx) -> Check:
    """The gate policy in the asset is the one the gate ran with: sha256(JCS(policy)) of policy/gate.yaml, in the
    form roadbed.gate writes, equals gate.json policy_sha256. The contamination threshold, num_perm and shingle that
    the near check uses come from this policy, so an edit to them must not pass silently."""
    label = "gate policy matches gate.json"
    live = jcs_sha256(c.asset.policy.model_dump(mode="json", exclude_none=True))
    stored = read_doc(c.d / "gate.json").get("policy_sha256")
    if live != stored:
        return Check("V-POLICY-MISMATCH", label, False,
                     f"{c.asset.cfg['gate']} hashes to {live[:12]}, {c.rrel}/gate.json policy_sha256 is {str(stored)[:12]}")
    return Check("V-POLICY-MISMATCH", label, True, f"{live[:12]} ({c.asset.cfg['gate']})")


def _datasets(c: _Ctx) -> Check:
    label = "datasets re-hashed"
    seal, snap = c.seal, c.snapshot
    assert seal is not None and snap is not None  # _binding ran first
    if not seal.get("merkle_root"):
        return Check("V-SEAL-INCOMPLETE", label, False, f"{c.seal_rel} is a Day 0 seal: no Merkle root, no MinHash, no disjointness check")
    hf = c.root / c.holdout_rel
    if sha256_file(hf) != seal["sha256"]:
        return Check("V-HASH-MISMATCH", label, False, f"{c.holdout_rel} does not match {c.seal_rel} sha256")
    hold = read_doc(hf)
    if len(hold) != seal["rows"]:
        return Check("V-HASH-MISMATCH", label, False, f"{c.holdout_rel} has {len(hold)} rows, {c.seal_rel} says {seal['rows']}")
    if any("row_hash" not in r for r in hold):
        return Check("V-HASH-MISMATCH", label, False, f"{c.holdout_rel}: a row without row_hash under a full seal")
    if merkle_root(holdout_row_hash(r) for r in hold) != seal["merkle_root"]:
        return Check("V-HASH-MISMATCH", label, False, f"{c.holdout_rel} Merkle root does not match {c.seal_rel}")
    c.holdout_rows = hold

    rows: list[dict[str, Any]] = []
    for i, part in enumerate(snap["parts"]):
        if part.get("path"):
            pr = part["path"]
            if not safe_rel(pr) or pr not in c.files:
                return Check("V-HASH-MISSING", label, False, f"snapshot part {pr} not found in the asset")
            data = (c.root / pr).read_bytes()
        elif c.store is not None and c.store.has(part["oid"]):
            try:
                data = c.store.get(part["oid"])
            except ObjectError as e:
                return Check("V-HASH-MISMATCH", label, False, str(e))
        else:
            return c.skip_or_fail("V-OBJECT-MISSING", label, f"snapshot part {i} ({part['oid'][:12]}) is in the bucket only; pass --objects")
        where = part.get("path") or f"object {part['oid'][:12]}"
        if sha256_bytes(data) != part["oid"] or len(data) != part["size"]:
            return Check("V-HASH-MISMATCH", label, False, f"snapshot part {where} does not match its oid or size")
        lines = [ln for ln in data.decode("utf-8").splitlines() if ln.strip()]
        if len(lines) != part["rows"]:
            return Check("V-HASH-MISMATCH", label, False, f"snapshot part {where} has {len(lines)} rows, manifest says {part['rows']}")
        for j, ln in enumerate(lines, 1):
            row = json.loads(ln)
            try:
                validate(row, "snapshot-row", strict=c.strict)
            except AssetValidationError as e:
                return Check("V-SCHEMA-INVALID", label, False, f"snapshot part {where} line {j}: {e}")
            if row_hash(row) != row["row_hash"]:
                return Check("V-HASH-MISMATCH", label, False, f"snapshot part {where} line {j}: row_hash does not match the row")
            rows.append(row)
    if len(rows) != snap["rows"] or merkle_root(r["row_hash"] for r in rows) != snap["merkle_root"]:
        return Check("V-HASH-MISMATCH", label, False, f"{c.snapshot_rel}: rows or Merkle root do not match its parts")
    c.train_rows = rows
    return Check("V-HASH-MISMATCH", label, True, f"{snap['id']} {len(rows)} rows, {seal['id']} {len(hold)} rows, Merkle roots match")


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _seal_order(c: _Ctx) -> Check:
    label = "seal precedes training: audit seq, timestamp"
    seal, p = c.seal, (c.receipt or {})["predicate"]
    assert seal is not None
    c.legs["audit_seq"] = seal["audit_seq"] < p["audit_seq"]
    c.legs["timestamp"] = _ts(seal["sealed_at"]) < _ts(p["started_at"])
    if not c.legs["audit_seq"]:
        return Check("V-SEAL-ORDER", label, False, f"seal audit seq {seal['audit_seq']} is not before training seq {p['audit_seq']}")
    if not c.legs["timestamp"]:
        return Check("V-SEAL-ORDER", label, False, f"sealed {seal['sealed_at']} is not before training started {p['started_at']}")
    return Check("V-SEAL-ORDER", label, True, f"seq {seal['audit_seq']} < {p['audit_seq']}, {seal['sealed_at']} < {p['started_at']}")


def _seal_git(c: _Ctx) -> Check:
    label = "seal precedes training: git ancestry"
    if not c.git:
        return c.skip_or_fail("V-SEAL-ORDER", label, "the asset is not a git clone; clone it (or its .bundle) to check ancestry")
    receipt_rel = f"{c.rrel}/{RECEIPT}"
    try:
        for rel in (c.seal_rel, receipt_rel):
            if not gitlog.matches_head(c.root, rel):
                return Check("V-SEAL-ORDER", label, False, f"{rel} differs from the committed file at HEAD")
        seal_commit = gitlog.added_in(c.root, c.seal_rel)
        train_commit = gitlog.added_in(c.root, receipt_rel)
        anc = seal_commit != train_commit and gitlog.is_ancestor(c.root, seal_commit, train_commit)
    except gitlog.GitError as e:
        return Check("V-SEAL-ORDER", label, False, str(e))
    c.legs.update(seal_commit=seal_commit, train_commit=train_commit, git_ancestor=anc)
    if not anc:
        return Check("V-SEAL-ORDER", label, False, f"seal commit {seal_commit[:12]} is not a strict ancestor of train commit {train_commit[:12]}")
    if (c.receipt or {})["predicate"]["holdout"]["seal_commit"] != seal_commit:
        return Check("V-SEAL-ORDER", label, False, f"receipt seal_commit is not the commit that added {c.seal_rel} ({seal_commit[:12]})")
    return Check("V-SEAL-ORDER", label, True, f"{seal_commit[:12]} < {train_commit[:12]}")


def _policy_contamination(c: _Ctx) -> tuple[float, int, int]:
    cp = c.asset.policy.contamination
    if cp is None:
        return DEFAULT_THRESHOLD, DEFAULT_NUM_PERM, DEFAULT_SHINGLE
    return cp.minhash_threshold, cp.num_perm, cp.shingle


def _exact(c: _Ctx) -> Check:
    label = "train and holdout disjoint: exact"
    if c.train_rows is None or c.holdout_rows is None:
        return c.skip_or_fail("V-HOLDOUT-OVERLAP", label, "training rows not available")
    ov = Overlap()
    exact_overlap(c.train_rows, c.holdout_rows, ov)
    c.overlap = ov
    if ov.exact_row_hashes:
        return Check("V-HOLDOUT-OVERLAP", label, False, f"{len(ov.exact_row_hashes)} row hashes in both, first {ov.exact_row_hashes[0][:12]}")
    if ov.shared_inputs:
        return Check("V-HOLDOUT-OVERLAP", label, False, f"{len(ov.shared_inputs)} inputs in both (same input_ref), first {ov.shared_inputs[0][7:19]}")
    return Check("V-HOLDOUT-OVERLAP", label, True, f"0 shared row hashes, 0 shared inputs ({len(c.train_rows)} x {len(c.holdout_rows)})")


def _texts(c: _Ctx, rows: list[dict[str, Any]]) -> list[tuple[str, str]] | None:
    if c.store is None:
        return None
    out = []
    for r in rows:
        oid = r["input_ref"].removeprefix("sha256:")
        if not c.store.has(oid):
            return None
        out.append((holdout_row_hash(r), c.store.get(oid).decode("utf-8")))
    return out


def _near(c: _Ctx) -> Check:
    threshold, num_perm, shingle = _policy_contamination(c)
    label = f"train and holdout disjoint: near (Jaccard < {threshold})"
    ov, seal = c.overlap, c.seal
    if ov is None or c.train_rows is None or c.holdout_rows is None or seal is None:
        return c.skip_or_fail("V-HOLDOUT-OVERLAP", label, "training rows not available")
    try:
        train = _texts(c, c.train_rows)
        if train is None:
            return c.skip_or_fail("V-OBJECT-MISSING", label, "raw training inputs are in the bucket only; pass --objects")
        hold = _texts(c, c.holdout_rows)
        if hold is not None:
            bad = _minhash_matches_raw(c, hold, num_perm, shingle)
            if bad is not None:
                return Check("V-HASH-MISMATCH", label, False, bad)
            near_exact(train, hold, threshold, shingle, ov)
        else:
            assert c.store is not None
            oid = seal["minhash_oid"]
            if not c.store.has(oid):
                return c.skip_or_fail("V-OBJECT-MISSING", label, f"neither raw holdout inputs nor the MinHash object {oid[:12]} are in --objects")
            n, k, sigs = parse_minhash_object(c.store.get(oid))
            if (n, k) != (num_perm, shingle):
                return Check("V-HOLDOUT-OVERLAP", label, False, f"MinHash object uses num_perm {n}, shingle {k}; policy says {num_perm}, {shingle}")
            if set(sigs) != {holdout_row_hash(r) for r in c.holdout_rows}:
                return Check("V-HOLDOUT-OVERLAP", label, False, "MinHash object does not cover exactly the sealed holdout rows")
            near_minhash(train, sigs, threshold, n, k, ov)
    except (ObjectError, UnicodeDecodeError, ValueError) as e:
        return Check("V-HASH-MISMATCH", label, False, str(e))
    if ov.near_pairs:
        p = ov.near_pairs[0]
        return Check("V-HOLDOUT-OVERLAP", label, False,
                     f"{len(ov.near_pairs)} near pairs, first holdout {p.holdout[:12]} ~ train {p.train[:12]} ({ov.near_method} {p.jaccard})")
    return Check("V-HOLDOUT-OVERLAP", label, True, f"{ov.near_method}, max {ov.max_jaccard}")


def _minhash_matches_raw(c: _Ctx, hold: list[tuple[str, str]], num_perm: int, shingle: int) -> str | None:
    """With the raw holdout inputs at hand, the sealed MinHash object (when --objects holds it) must be their MinHash
    under the policy's num_perm and shingle: pre-train checks that only see the object rely on it. A missing object
    is reported by the bucket-objects check."""
    assert c.store is not None and c.seal is not None
    oid = c.seal.get("minhash_oid")
    if not oid or not c.store.has(oid):
        return None
    n, k, sigs = parse_minhash_object(c.store.get(oid))
    if (n, k) != (num_perm, shingle):
        return f"MinHash object {oid[:12]} uses num_perm {n}, shingle {k}; policy says {num_perm}, {shingle}"
    want = {h: minhash(shingles(text, k), n) for h, text in hold}
    if sigs != want:
        return f"MinHash object {oid[:12]} does not match the MinHash of the raw holdout inputs"
    return None


def _contamination(c: _Ctx) -> Check:
    """contamination.json and the gate's contamination check say what verify recomputed."""
    label = "contamination.json matches"
    f = f"{c.rrel}/contamination.json"
    doc = read_doc(c.root / f)
    seal, snap, ov = c.seal or {}, c.snapshot or {}, c.overlap

    def bad(msg: str) -> Check:
        return Check("V-CONTAMINATION-MISMATCH", label, False, f"{f}: {msg}")

    if doc["rebase"] != c.n:
        return bad(f"rebase {doc['rebase']}")
    if (doc["holdout"]["id"], doc["holdout"]["merkle_root"]) != (seal.get("id"), seal.get("merkle_root")):
        return bad("holdout differs from SEAL.json")
    if (doc["train"]["id"], doc["train"]["merkle_root"]) != (snap.get("id"), snap.get("merkle_root")):
        return bad("train differs from the snapshot manifest")
    if ov is not None and (doc["exact"]["shared"] != len(ov.exact_row_hashes) or sorted(doc["exact"]["row_hashes"]) != ov.exact_row_hashes):
        return bad("exact overlap differs from the recomputed one")
    threshold, num_perm, _ = _policy_contamination(c)
    for key, want in (("threshold", threshold), ("num_perm", num_perm)):
        if doc["near"][key] != want:
            return bad(f"near.{key} is {doc['near'][key]}, {c.asset.cfg['gate']} says {want}")
    if ov is not None and ov.near_method == doc["near"]["method"]:
        got = sorted((p["holdout"], p["train"]) for p in doc["near"]["pairs"])
        if got != [(p.holdout, p.train) for p in ov.near_pairs]:
            return bad("near pairs differ from the recomputed ones")
    so = doc["seal_order"]
    for leg in ("audit_seq", "timestamp", "git_ancestor", "seal_commit", "train_commit"):
        if leg in c.legs and so[leg] != c.legs[leg]:
            return bad(f"seal_order.{leg} is {so[leg]}, recomputed {c.legs[leg]}")
    clean = ov is not None and ov.clean and all(c.legs.get(k, True) for k in ("audit_seq", "timestamp", "git_ancestor"))
    if clean and doc["status"] != "pass":
        return bad(f"status {doc['status']}, recomputed pass")
    gate = read_doc(c.d / "gate.json")
    checks = {ch["name"]: ch["status"] for ch in gate["checks"]}
    gc = checks.get("contamination")
    if gc is None or (doc["status"] == "pass") != (gc == "pass"):
        return Check("V-CONTAMINATION-MISMATCH", label, False, f"{c.rrel}/gate.json contamination check is {gc or 'missing'}, {f} says {doc['status']}")
    if "seal_order" in checks and checks["seal_order"] == "pass" and not so["ok"]:
        return Check("V-CONTAMINATION-MISMATCH", label, False, f"{c.rrel}/gate.json seal_order passes but {f} says it does not hold")
    return Check("V-CONTAMINATION-MISMATCH", label, True, f"status {doc['status']}, gate check {checks['contamination']}")


def _sod(c: _Ctx) -> Check:
    label = "segregation of duties (SoD-01, SoD-02)"
    meta, seal = c.meta or {}, c.seal or {}
    requester = meta.get("requested_by")
    if requester is None:
        return c.skip_or_fail("V-SOD", label, "rebase.yaml names no requester")
    if seal.get("sealed_by") == requester:
        return Check("V-SOD", label, False, f"SoD-01: {requester} sealed the holdout and requested the run")
    ap = c.d / EVIDENCE / "approvals.json"
    if ap.is_file():
        doc = read_doc(ap)
        if doc["rebase"] != c.n:
            return Check("V-SOD", label, False, f"approvals.json is for rebase {doc['rebase']}")
        entries = [(a, e["role"]) for a, e in doc["approvals"].items()] + [(e["actor"], e["role"]) for e in doc.get("history", [])]
        if any(a == requester and role == "approver" for a, role in entries):
            return Check("V-SOD", label, False, f"SoD-02: {requester} requested the rebase and approves it")
    return Check("V-SOD", label, True, f"requester {requester}, sealer {seal.get('sealed_by')}")


def _objects(c: _Ctx) -> Check:
    """Every bucket object the asset names (lock objects: pointers, snapshot parts, MinHash objects, adapters, `oid:`
    receipt subjects) is in --objects; the lock check re-hashed those present. A missing one is a skip, never a pass."""
    label = "bucket objects re-hashed"
    named: dict[str, str] = {}
    for o in named_objects(c.root, c.files):
        named.setdefault(o.oid, o.ref)
    if c.store is None:
        return c.skip_or_fail("V-OBJECT-MISSING", label, f"{len(named)} objects named by the asset not re-hashed; pass --objects")
    missing = sorted(oid for oid in named if not c.store.has(oid))
    if missing:
        first = ", ".join(f"{oid[:12]} (named by {named[oid]})" for oid in missing[:3])
        return c.skip_or_fail("V-OBJECT-MISSING", label, f"{len(missing)} of {len(named)} objects are not in --objects: {first}")
    return Check("V-OBJECT-MISSING", label, True, f"{len(named)} objects")


# ---------------------------------------------------------------- driver


def run(
    asset: Asset,
    d: Path,
    n: int,
    on_check: Callable[[Check], None],
    *,
    objects: str | Path | None = None,
    strict: bool = False,
    trusted_keyids: Collection[str] | None = None,
) -> list[Check]:
    """Run the checks in order. Calls on_check(check) for each. Raises VerifyFailed on the first failure."""
    store = ObjectStore(objects) if objects is not None else None
    trusted = frozenset(trusted_keyids) if trusted_keyids is not None else None
    steps: list[Callable[[], Check]]
    if not is_full(asset.root, d):
        steps = [lambda: _hashes(asset, d), lambda: _schemas(d), lambda: _scores(asset, d, n), lambda: _seal(asset, d)]
    else:
        holder: list[_Ctx] = []

        def ctx() -> _Ctx:
            if not holder:
                holder.append(_Ctx(asset, d, n, store, strict, trusted))
            return holder[0]

        steps = [
            lambda: _hashes(asset, d, _required(ctx())),
            lambda: _lock(ctx()),
            lambda: _all_schemas(ctx()),
            lambda: _synthetic(ctx()),
            lambda: _pii(ctx()),
            lambda: _signatures(ctx()),
            lambda: _evidence(ctx()),
            lambda: _chain(ctx()),
            lambda: _binding(ctx()),
            lambda: _policy(ctx()),
            lambda: _schemas(d),
            lambda: _scores(asset, d, n),
            lambda: _seal(asset, d),
            lambda: _datasets(ctx()),
            lambda: _seal_order(ctx()),
            lambda: _seal_git(ctx()),
            lambda: _config(ctx()),
            lambda: _exact(ctx()),
            lambda: _near(ctx()),
            lambda: _contamination(ctx()),
            lambda: _sod(ctx()),
            lambda: _objects(ctx()),
        ]
    done: list[Check] = []
    for fn in steps:
        try:
            c = fn()
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as e:
            c = Check("V-CHECK-ERROR", "", False, f"{type(e).__name__}: {e}")
        done.append(c)
        on_check(c)
        if c.ok is False:
            raise VerifyFailed(c)
    return done


def verify(
    rebase_dir: str | Path,
    *,
    asset_root: str | Path | None = None,
    objects: str | Path | None = None,
    strict: bool = False,
    trusted_keyids: Collection[str] | None = None,
    on_check: Callable[[Check], None] | None = None,
) -> VerifyResult:
    """Verify one rebase directory. Returns every check run, in order, and the first failed one (or None).

    asset_root defaults to the rebase directory's grandparent (rebases/N). objects is a local directory laid out as
    the bucket (objects/sha256/ab/cd/<oid>) holding snapshot parts, raw inputs or the MinHash object.
    A roadbed.yaml or policy file that does not conform is a failed V-SCHEMA-INVALID check. Raises
    roadbed.cli.asset.AssetError when there is no asset at all.
    """
    d = Path(rebase_dir).resolve()
    root = Path(asset_root).resolve() if asset_root is not None else d.parent.parent
    if not d.is_relative_to(root):
        raise ValueError(f"{d} is not inside the asset {root}")
    if not re.fullmatch(r"\d+", d.name):
        raise ValueError(f"rebase directory must be named by its number, e.g. rebases/13 (got {d.name})")
    n = int(d.name)
    try:
        asset = open_asset(root)
    except (jsonschema.ValidationError, PydanticError) as e:
        # roadbed.yaml or a policy file it names does not conform; the asset cannot be read further.
        msg = e.message if isinstance(e, jsonschema.ValidationError) else str(e).splitlines()[0]
        bad = Check("V-SCHEMA-INVALID", "every file matches its schema", False, f"asset config: {msg}")
        if on_check is not None:
            on_check(bad)
        return VerifyResult(n, True, [bad], bad)
    checks: list[Check] = []

    def collect(c: Check) -> None:
        checks.append(c)
        if on_check is not None:
            on_check(c)

    try:
        run(asset, d, n, collect, objects=objects, strict=strict, trusted_keyids=trusted_keyids)
    except VerifyFailed as e:
        return VerifyResult(n, is_full(root, d), checks, e.check)
    return VerifyResult(n, is_full(root, d), checks, None)
