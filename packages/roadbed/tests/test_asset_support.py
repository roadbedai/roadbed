"""Shared helpers for the asset tests: the fixture builder, bundle clones, a bucket of raw inputs, and an
"attacker with the keys" that re-hashes and re-signs a tampered asset so only the check under test can catch it.

The signing keys are the fixture TEST KEYS from spec/tools/build_examples.py (derived from public strings).
No tests live here.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from roadbed.asset import ObjectStore, build_lock, sign_file, write_lock, write_signature
from roadbed.asset.files import parse_manifest
from roadbed.asset.overlap import minhash, minhash_object, shingles
from roadbed.hashing import sha256_file

SPEC_DIR = Path(__file__).resolve().parents[3] / "spec"
EXAMPLES = SPEC_DIR / "examples"
CONFORMANCE = SPEC_DIR / "conformance"
REBASE = {"pass": 13, "blocked": 14}


@cache
def builder() -> ModuleType:
    """spec/tools/build_examples.py as a module: its narrative() and verdict_row() made the fixture inputs."""
    path = SPEC_DIR / "tools" / "build_examples.py"
    spec = importlib.util.spec_from_file_location("roadbed_build_examples", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def git(root: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@roadbed.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@roadbed.invalid", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
           "HOME": str(root)}
    r = subprocess.run(["git", "-c", "commit.gpgsign=false", "-C", str(root), *args], capture_output=True, text=True,
                       env=env, check=True)
    return r.stdout.strip()


def clone(name: str, dest: Path) -> Path:
    """git clone spec/examples/<name>.bundle: the asset with its real history."""
    subprocess.run(["git", "clone", "-q", str(EXAMPLES / f"{name}.bundle"), str(dest)], check=True, capture_output=True)
    return dest


def copy(name: str, dest: Path) -> Path:
    """A plain copy (no git history)."""
    shutil.copytree(EXAMPLES / name, dest)
    return dest


def _lines(b: ModuleType) -> dict[str, list[str]]:
    return {
        "h": ["bodily_injury" if i < 30 else b.OTHER_LINES[i % 2] for i in range(60)],
        "t": [["bodily_injury", *b.OTHER_LINES][i % 3] for i in range(80)],
        "oh": [["bodily_injury", *b.OTHER_LINES][i % 3] for i in range(60)],
        "os": [["bodily_injury", *b.OTHER_LINES][i % 3] for i in range(74)],
    }


def bucket(dest: Path, *, holdout_raw: bool = True) -> ObjectStore:
    """A local bucket with every object the fixtures name that is not an adapter: the raw inputs (by input_ref),
    the bucket-only snapshot part of S-2026-10-15 and both MinHash objects. Rebuilt from the builder's
    narratives, so the oids are known answers (they must equal the ones the fixtures name).

    holdout_raw=False leaves out the raw holdout inputs, so verify must fall back to the MinHash object.
    """
    b = builder()
    store = ObjectStore(dest)
    lines = _lines(b)
    for prefix, n in (("h", 60), ("t", 80), ("oh", 60), ("os", 74)):
        if prefix in ("h", "oh") and not holdout_raw:
            continue
        for i in range(n):
            store.put(b.narrative(prefix, i).encode())
    osnap = [b.verdict_row("os", i, lines["os"][i]) for i in range(74)]
    store.put("".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in osnap).encode())
    for prefix, day in (("h", 1), ("oh", -1)):
        rows = [b.verdict_row(prefix, i, lines[prefix][i], day=day) for i in range(60)]
        sigs = {r["row_hash"]: minhash(shingles(b.narrative(prefix, i))) for i, r in enumerate(rows)}
        store.put(minhash_object(sigs))
    return store


def manifest_text(root: Path, paths: list[str]) -> str:
    return "".join(f"{sha256_file(root / p)}  {p}\n" for p in paths)


def _rehash(root: Path, text: str) -> str:
    """Re-hash every manifest line whose file exists; keep the line of a missing file as it was."""
    out = []
    for e in parse_manifest(text):
        f = root / e["path"]
        out.append(f"{sha256_file(f) if f.is_file() else e['sha256']}  {e['path']}\n")
    return "".join(out)


def _rebind_config(root: Path, receipts: list[Path]) -> None:
    """Re-hash the five config files into each receipt that has predicate.config. lock_sha256 stays: it names the
    lock the job started from, which a later relock does not change."""
    cfg = yaml.safe_load((root / "roadbed.yaml").read_text())
    paths = {"roadbed_yaml_sha256": "roadbed.yaml", "slices_sha256": cfg["evals"]["slices"],
             "scorers_sha256": cfg["evals"]["scorers"], "gate_policy_sha256": cfg["gate"],
             "seal_sha256": f"{str(cfg['data']['holdout']['path']).rstrip('/')}/SEAL.json"}
    for p in receipts:
        doc = json.loads(p.read_text())
        config = doc["predicate"].get("config")
        if not config:
            continue
        new = {**config, **{k: sha256_file(root / rel) for k, rel in paths.items() if k in config}}
        if new != config:
            doc["predicate"]["config"] = new
            p.write_text(json.dumps(doc, indent=2) + "\n")


def reseal(root: Path, n: int, *, chain: bool = False, commit: str | None = None, store: ObjectStore | None = None,
           sign: bool = True, relock: bool = True, rebind: bool = True) -> None:
    """Make a tampered asset self-consistent again, as an attacker who holds the signing keys would: re-chain and
    re-bind the receipt config digests, re-sign the receipts, re-hash the rebase manifest, re-hash and re-sign the evidence manifest, rewrite the lock.
    With commit, also commit the result (git clones)."""
    b = builder()
    rkey, ekey = b.test_key("receipt"), b.test_key("evidence")
    receipts = sorted(root.glob("versions/*/receipt.json")) + sorted(root.glob("rebases/*/receipt.json"))
    if rebind:
        _rebind_config(root, receipts)
    if chain:
        docs = sorted(((json.loads(p.read_text()), p) for p in receipts), key=lambda x: x[0]["predicate"]["started_at"])
        prev = None
        for doc, p in docs:
            doc["predicate"]["prev_receipt"] = prev
            p.write_text(json.dumps(doc, indent=2) + "\n")
            prev = b.jcs_sha256(doc)
    for p in receipts if sign else []:
        rel = p.relative_to(root).as_posix()
        write_signature(p.with_name("receipt.sig"), sign_file(rkey, root, rel, "policy/keys/receipt.pub", "jcs"))
    m = root / f"rebases/{n}/MANIFEST.sha256"
    if m.is_file():
        m.write_text(_rehash(root, m.read_text()))
    em = root / f"rebases/{n}/evidence/MANIFEST.sha256"
    if em.is_file():
        em.write_text(_rehash(root, em.read_text()))
        rel = em.relative_to(root).as_posix()
        if sign:
            write_signature(em.with_name("MANIFEST.sig"), sign_file(ekey, root, rel, "policy/keys/evidence.pub", "raw"))
    lock = root / "roadbed.lock"
    if relock and lock.is_file():
        write_lock(root, build_lock(root, store=store, previous=json.loads(lock.read_text()), synthetic=True))
    if commit:
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", commit)


def edit_json(path: Path, fn: Any) -> None:
    doc = json.loads(path.read_text())
    out = fn(doc)
    path.write_text(json.dumps(doc if out is None else out, indent=2) + "\n")


# ---------------------------------------------------------------- a local asset (the free path, W01d)

LOCAL_HID = "H-2026-11-02"
LOCAL_N = 1


def local_asset(dest: Path, *, rebase: bool = False, events: Path | None = None) -> Path:
    """An asset built on the free path, in dest/acme (a git repo): roadbed new --synthetic over the acme-2k events,
    commit, evals build (candidate in dest/vault, store in dest/objects), evals seal, commit. events: another events
    directory for the build (default the acme-2k events). With rebase: stand-in
    predictions from the holdout's expected outputs (candidate exact, incumbent wrong on every fifth severity),
    rebase --from-predictions as rebases/1 one minute after the seal, commit. Returns the asset root."""
    from test_evals_support import FIXTURE
    from typer.testing import CliRunner

    from roadbed.cli.asset import open_asset
    from roadbed.cli.main import app
    from roadbed.cli.rebase import BaseRef
    from roadbed.cli.rebase import run as rebase_run

    root, cli = dest / "acme", CliRunner()
    steps = [
        ["new", str(root), "--model", "claims-triage", "--synthetic", "--slices", str(FIXTURE / "slices.yaml"),
         "--output-schema", str(FIXTURE / "output.schema.json")],
        ["evals", "build", "--asset", str(root), "--events", str(events or FIXTURE), "--as-of", "2026-11-02T09:00:00Z",
         "--seed", "7", "--holdout-out", str(dest / "vault"), "--objects", str(dest / "objects")],
        ["evals", "seal", "--asset", str(root), "--candidate", str(dest / "vault"), "--sealed-by", "vik",
         "--objects", str(dest / "objects")],
    ]
    for i, args in enumerate(steps):
        r = cli.invoke(app, args)
        assert r.exit_code == 0, r.output
        if i == 0:
            git(root, "init", "-q")
        if i != 1:
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "asset" if i == 0 else "seal")
    if rebase:
        d = root / f"rebases/{LOCAL_N}"
        d.mkdir(parents=True)
        hold = [json.loads(x) for x in (root / f"data/holdout/{LOCAL_HID}/holdout.jsonl").read_text().splitlines()]
        cand = [{"id": h["id"], "output": h["expected"]} for h in hold]
        inc = [{"id": h["id"], "output": {**h["expected"], "severity": 1 + h["expected"]["severity"] % 5} if i % 5 == 0 else h["expected"]}
               for i, h in enumerate(hold)]
        for name, rows in (("candidate", cand), ("incumbent", inc)):
            (d / f"predictions.{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        sealed_at = json.loads((root / f"data/holdout/{LOCAL_HID}/SEAL.json").read_text())["sealed_at"]
        created = (datetime.fromisoformat(sealed_at.replace("Z", "+00:00")) + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (d / "rebase.yaml").write_text(f"created_at: '{created}'\n")
        rebase_run(open_asset(root), d, LOCAL_N, BaseRef("Qwen/Qwen3-8B", license="apache-2.0"), emit=lambda _: None)
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "rebase 1")
    return root
