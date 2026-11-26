"""Loading an asset repo and the files of one rebase."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from roadbed.contracts import GatePolicy, HoldoutRow, LicensePolicy, PredictionRow, Receipt, Scorers, Slices
from roadbed.io import load_asset, read_jsonl, read_yaml
from roadbed.scoring import load_scorers

NOTICE = "predictions supplied via --from-predictions; no training in this run"
# Executors that never train: their predictions count as supplied (contracts.ExecutorName).
NO_TRAINING_EXECUTORS = ("mock", "replay")
LICENSE_POLICY = "policy/license.yaml"
PRED_FILES = {"incumbent": "predictions.incumbent.jsonl", "candidate": "predictions.candidate.jsonl"}
MANIFEST = "MANIFEST.sha256"
REBASE_META = "rebase.yaml"
PROPOSED = "roadbed.yaml.proposed"
SEAL_NAMES = ("SEAL.yaml", "SEAL.yml", "SEAL.json", "SEAL")
# roadbed.yaml fields that name a file the CLI reads; each must stay inside the asset.
PATH_FIELDS = (("gate",), ("evals", "scorers"), ("evals", "slices"), ("task", "output_schema"))


class AssetError(Exception):
    """Input problem the user can fix. Exit code 1."""


@dataclass
class Asset:
    root: Path
    cfg: dict[str, Any]
    policy: GatePolicy
    scorers: Scorers
    slices: Slices
    license_policy: LicensePolicy | None

    @property
    def holdout_dir(self) -> Path:
        return self.root / self.cfg["data"]["holdout"]["path"]

    @property
    def holdout_file(self) -> Path:
        return self.holdout_dir / "holdout.jsonl"

    def rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root.resolve()).as_posix()


def find_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "roadbed.yaml").is_file():
            return p
    raise AssetError(f"no roadbed.yaml in {start} or its parents; run inside an asset repo or pass --asset")


def refuse_symlinks(root: Path) -> None:
    """Raise AssetError naming the first symlink among the asset's files and the directories on their paths."""
    from roadbed.asset.files import tree_files  # roadbed.asset imports this module (verify)

    for rel in tree_files(root):
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):
            if (root.joinpath(*parts[:i])).is_symlink():
                raise AssetError(f"{'/'.join(parts[:i])} is a symlink: an asset holds plain files only")


def refuse_outside_paths(cfg: Any) -> None:
    """Raise AssetError when a PATH_FIELDS value of roadbed.yaml leaves the asset. The message never shows the value."""
    from roadbed.asset.files import safe_rel  # roadbed.asset imports this module (verify)

    for field in PATH_FIELDS:
        node: Any = cfg
        for k in field:
            node = node.get(k) if isinstance(node, dict) else None
        if node is not None and not (isinstance(node, str) and safe_rel(node)):
            raise AssetError(f"roadbed.yaml {'.'.join(field)} must be a relative path inside the asset")


def open_asset(root: Path, *, plain_files: bool = True) -> Asset:
    """Load the asset at root. It always refuses a roadbed.yaml file path that leaves the asset. plain_files=False skips
    only the symlink refusal: verify passes it and reports a symlink itself, as a V-PATH-UNSAFE failure."""
    root = root.resolve()
    if plain_files:
        refuse_symlinks(root)
    if not (root / "roadbed.yaml").is_file():
        raise AssetError(f"no roadbed.yaml in {root}")
    refuse_outside_paths(read_yaml(root / "roadbed.yaml"))
    cfg = load_asset(root)
    lic_path = root / LICENSE_POLICY
    return Asset(
        root=root,
        cfg=cfg,
        policy=GatePolicy.model_validate(read_yaml(root / cfg["gate"])),
        scorers=load_scorers(root / cfg["evals"]["scorers"]),
        slices=Slices.model_validate(read_yaml(root / cfg["evals"]["slices"])),
        license_policy=LicensePolicy.model_validate(read_yaml(lic_path)) if lic_path.is_file() else None,
    )


def rebase_dir(asset_root: Path, arg: str) -> tuple[Path, int]:
    p = Path(arg)
    d = p if p.is_absolute() else asset_root / p
    if not d.is_dir():
        # also accept a path relative to the current directory
        if p.is_dir():
            d = p.resolve()
        else:
            raise AssetError(f"rebase directory not found: {arg}")
    m = re.fullmatch(r"\d+", d.name)
    if not m:
        raise AssetError(f"rebase directory must be named by its number, e.g. rebases/13 (got {d.name})")
    return d, int(d.name)


def load_rows(asset: Asset, d: Path) -> tuple[list[dict], list[dict], list[dict]]:
    if not asset.holdout_file.is_file():
        raise AssetError(f"holdout not found: {asset.rel(asset.holdout_file)}")
    hold = read_jsonl(asset.holdout_file)
    for r in hold:
        HoldoutRow.model_validate(r)
    preds = []
    for name in ("incumbent", "candidate"):
        f = d / PRED_FILES[name]
        if not f.is_file():
            raise AssetError(f"prediction file not found: {f}")
        rows = read_jsonl(f)
        for r in rows:
            PredictionRow.model_validate(r)
        preds.append(rows)
    return hold, preds[0], preds[1]


def receipt_executor(d: Path) -> str | None:
    """Executor name from rebases/<n>/receipt.json, or None when the rebase has no receipt."""
    f = d / "receipt.json"
    if not f.is_file():
        return None
    receipt = Receipt.model_validate(json.loads(f.read_text(encoding="utf-8")))
    return str(receipt.predicate.executor.name)


def predictions_supplied(executor: str | None) -> bool:
    """True unless a training executor (runner, hf-jobs, together) produced the predictions."""
    return executor is None or executor in NO_TRAINING_EXECUTORS


def read_meta(d: Path) -> dict[str, Any] | None:
    f = d / REBASE_META
    return read_yaml(f) if f.is_file() else None


def seal_file(asset: Asset) -> Path | None:
    for name in SEAL_NAMES:
        f = asset.holdout_dir / name
        if f.is_file():
            return f
    return None


def bound_files(asset: Asset) -> list[Path]:
    """Files a rebase MANIFEST must also list (SPEC.md, Manifests): the seal, the training snapshot manifest, the recipe.

    Only files that exist are returned. The snapshot S-<date> lives in <data.verdicts>/<date>/MANIFEST.json.
    """
    out = []
    if (f := seal_file(asset)) is not None:
        out.append(f)
    data = asset.cfg["data"]
    if data.get("verdicts") and data.get("snapshot"):
        f = asset.root / data["verdicts"] / data["snapshot"].removeprefix("S-") / "MANIFEST.json"
        if f.is_file():
            out.append(f)
    f = asset.root / asset.cfg["recipe"]
    if f.is_file():
        out.append(f)
    return out


def seal_time(asset: Asset) -> Any:
    """sealed_at from a SEAL file in the holdout directory, else from roadbed.yaml."""
    for name in SEAL_NAMES:
        f = asset.holdout_dir / name
        if f.is_file():
            data = read_yaml(f)  # YAML is a superset of JSON
            if isinstance(data, dict) and "sealed_at" in data:
                return data["sealed_at"]
    return asset.cfg["data"]["holdout"]["sealed_at"]
