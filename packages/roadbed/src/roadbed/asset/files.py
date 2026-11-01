"""Load and validate the files of an asset tree against the v0.1 schemas (SPEC.md, Files and schemas)."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
from pydantic import ValidationError as PydanticError

from roadbed.contracts import SCHEMA_MODELS, RoadbedConfig, json_schema
from roadbed.io import read_jsonl, read_yaml

LOCK = "roadbed.lock"
MANIFEST = "MANIFEST.sha256"
SYNTHETIC_MD = "SYNTHETIC.md"

# Path pattern (from the asset root) -> schema name. First match wins. JSONL files validate per line.
# MANIFEST.sha256 files are text and are parsed into the evidence-manifest shape first.
SCHEMA_RULES: tuple[tuple[str, str], ...] = (
    ("roadbed.yaml", "roadbed"),
    (LOCK, "lock"),
    ("*.ptr", "object-pointer"),
    ("policy/gate.yaml", "gate-policy"),
    ("policy/license.yaml", "license-policy"),
    ("data/snapshots/*/MANIFEST.json", "snapshot-manifest"),
    ("data/snapshots/*/*.jsonl", "snapshot-row"),
    ("data/holdout/*/SEAL.json", "seal"),
    ("data/holdout/*/holdout.jsonl", "holdout-row"),
    ("data/tombstones.jsonl", "tombstone-row"),
    ("evals/golden.jsonl", "golden-row"),
    ("evals/correction_tests.jsonl", "correction-test-row"),
    ("evals/scorers.yaml", "scorers"),
    ("evals/slices.yaml", "slices"),
    ("recipes/*/*.yaml", "recipe"),
    ("versions/*/model.json", "model-version"),
    ("versions/*/receipt.json", "receipt"),
    ("rebases/*/receipt.json", "receipt"),
    ("*.sig", "signature"),
    ("rebases/*/rebase.yaml", "rebase"),
    ("rebases/*/predictions.*.jsonl", "prediction-row"),
    ("rebases/*/scores.json", "scores"),
    ("rebases/*/gate.json", "gate"),
    ("rebases/*/contamination.json", "contamination"),
    ("rebases/*/cost.json", "cost"),
    ("rebases/*/license.json", "license-decision"),
    ("rebases/*/roadbed.yaml.proposed", "roadbed"),
    ("rebases/*/evidence/approvals.json", "approvals"),
    ("rebases/*/MANIFEST.sha256", "evidence-manifest"),
    ("rebases/*/evidence/MANIFEST.sha256", "evidence-manifest"),
    ("findings/F-*.yaml", "finding"),
    ("lineage/edges.jsonl", "lineage-edge"),
    ("audit/anchors/*.json", "audit-anchor"),
)


class AssetValidationError(Exception):
    """A file does not conform to its schema. The message names the file and the first problem."""


def schema_for(rel: str) -> str | None:
    """Schema name of a path from the asset root, or None for files without a schema (PDF, report.md, keys)."""
    for pattern, name in SCHEMA_RULES:
        # fnmatch's * crosses "/", so compare segment counts to keep "recipes/*/*.yaml" to two levels.
        if fnmatch.fnmatchcase(rel, pattern) and (pattern.startswith("*") or rel.count("/") == pattern.count("/")):
            return name
    return None


def _iso(obj: Any) -> str:
    if hasattr(obj, "isoformat"):
        return obj.isoformat().replace("+00:00", "Z")
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")


def read_doc(path: str | Path) -> Any:
    """Read a JSON, YAML, JSONL or MANIFEST.sha256 file into plain JSON values.

    JSONL returns a list of rows. A MANIFEST returns {"entries": [{"sha256", "path"}, ...]}.
    YAML dates become RFC 3339 strings, as load_asset does, so JSON Schema sees strings.
    """
    p = Path(path)
    if p.name.endswith(".jsonl"):
        return read_jsonl(p)
    if p.name == MANIFEST:
        return {"entries": parse_manifest(p.read_text(encoding="utf-8"))}
    if p.suffix in (".yaml", ".yml", ".proposed"):
        return json.loads(json.dumps(read_yaml(p), default=_iso))
    return json.loads(p.read_text(encoding="utf-8"))


def parse_manifest(text: str) -> list[dict[str, str]]:
    """Parse `<sha256>  <path>` lines (SPEC.md, Manifests). Raises ValueError on a malformed line."""
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        digest, sep, rel = line.partition("  ")
        if not sep or len(digest) != 64 or not rel:
            raise ValueError(f"line {lineno} is not '<sha256>  <path>'")
        out.append({"sha256": digest, "path": rel})
    return out


@cache
def _validator(name: str, strict: bool) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(
        json_schema(name, reader=not strict), format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )


def validate(data: Any, schema: str, *, strict: bool = False) -> None:
    """Validate one JSON value against a v0.1 schema and its Pydantic model.

    strict=False is the reader form (unknown fields allowed, SPEC.md compatibility); strict=True is the
    published closed schema that producers must meet. The model adds the rules JSON Schema cannot express.
    Raises AssetValidationError.
    """
    v = _validator(schema, strict)
    err = jsonschema.exceptions.best_match(v.iter_errors(data))
    if err is not None:
        where = "/".join(str(x) for x in err.absolute_path)
        raise AssetValidationError(f"{where + ': ' if where else ''}{err.message}")
    try:
        SCHEMA_MODELS[schema].model_validate(data)
    except PydanticError as e:
        first = e.errors()[0]
        where = "/".join(str(x) for x in first["loc"])
        raise AssetValidationError(f"{where + ': ' if where else ''}{first['msg']}") from None


def validate_file(root: Path, rel: str, *, strict: bool = False) -> str | None:
    """Validate one file by its path. Returns the schema name used, or None when the path has no schema."""
    name = schema_for(rel)
    if name is None:
        return None
    try:
        data = read_doc(root / rel)
    except (OSError, ValueError) as e:  # JSONDecodeError and YAML errors are ValueErrors
        raise AssetValidationError(f"{rel}: {e}") from None
    except Exception as e:  # ruamel raises its own error types
        raise AssetValidationError(f"{rel}: {type(e).__name__}: {e}") from None
    rows = data if rel.endswith(".jsonl") else [data]
    for i, row in enumerate(rows, 1):
        try:
            validate(row, name, strict=strict)
        except AssetValidationError as e:
            raise AssetValidationError(f"{rel}{f' line {i}' if rel.endswith('.jsonl') else ''}: {e}") from None
    return name


def safe_rel(rel: str) -> bool:
    """True for a plain relative '/' path that stays inside the asset: no absolute path, no '..', no empty part."""
    return bool(rel) and not rel.startswith("/") and "\\" not in rel and all(p not in ("", ".", "..") for p in rel.split("/"))


def tree_files(root: Path) -> list[str]:
    """Every file under root except .git/, as '/' paths sorted by their UTF-8 bytes (SPEC.md, Hashing rule 7)."""
    out = []
    for p in root.rglob("*"):
        rel = p.relative_to(root).as_posix()
        if ".git" in p.relative_to(root).parts:
            continue
        if p.is_symlink():
            out.append(rel)  # listed so the lock check sees it; verify refuses symlinks
        elif p.is_file():
            out.append(rel)
    return sorted(out, key=lambda s: s.encode("utf-8"))


@dataclass
class AssetTree:
    """A loaded asset: its root, the validated roadbed.yaml (as stored) and its model."""

    root: Path
    cfg: dict[str, Any]
    config: RoadbedConfig

    def path(self, rel: str) -> Path:
        return self.root / rel

    def rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root.resolve()).as_posix()

    @property
    def holdout_dir(self) -> str:
        return str(self.cfg["data"]["holdout"]["path"]).rstrip("/")

    def snapshot_manifest(self, snapshot_id: str) -> str | None:
        """Path of snapshot S-<date>: <data.verdicts>/<date>/MANIFEST.json."""
        verdicts = self.cfg["data"].get("verdicts")
        if not verdicts or not snapshot_id.startswith("S-"):
            return None
        return f"{str(verdicts).rstrip('/')}/{snapshot_id.removeprefix('S-')}/MANIFEST.json"


def load(root: str | Path) -> AssetTree:
    """Load <root>/roadbed.yaml and validate it (reader form). Raises AssetValidationError or FileNotFoundError."""
    r = Path(root).resolve()
    f = r / "roadbed.yaml"
    if not f.is_file():
        raise FileNotFoundError(f"no roadbed.yaml in {r}")
    cfg = read_doc(f)
    try:
        validate(cfg, "roadbed")
    except AssetValidationError as e:
        raise AssetValidationError(f"roadbed.yaml: {e}") from None
    return AssetTree(root=r, cfg=cfg, config=RoadbedConfig.model_validate(cfg))
