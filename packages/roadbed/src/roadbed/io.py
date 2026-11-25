"""File IO for the asset format: JSONL, YAML, and roadbed.yaml loading."""

from __future__ import annotations

import errno
import io as _io
import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import jsonschema
from ruamel.yaml import YAML

from roadbed.contracts import json_schema


def _yaml() -> YAML:
    y = YAML(typ="safe", pure=True)
    y.default_flow_style = False
    y.sort_base_mapping_type_on_output = False  # type: ignore[assignment]  # ruamel's stub types it as None
    return y


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read one JSON object per line. Blank lines are skipped."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{lineno}: expected a JSON object")
            rows.append(row)
    return rows


def write_nofollow(path: str | Path, data: bytes) -> None:
    """Write data to path, never through a symlink: a link at path (also a dangling one) raises ValueError."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o666)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise ValueError(f"{Path(path).name} is a symlink: an asset holds plain files only") from None
        raise
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    """Write one compact JSON object per line, keys in insertion order, trailing newline. Never through a symlink."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_nofollow(p, "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows).encode("utf-8"))


def read_yaml(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return _yaml().load(f)


def write_yaml(path: str | Path, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    buf = _io.StringIO()
    _yaml().dump(data, buf)
    write_nofollow(p, buf.getvalue().encode("utf-8"))


def load_asset(root: str | Path) -> dict[str, Any]:
    """Load <root>/roadbed.yaml and validate it against roadbed.schema.json.

    Raises jsonschema.ValidationError when the file does not conform.
    """
    data = read_yaml(Path(root) / "roadbed.yaml")
    # YAML may parse an unquoted timestamp as datetime; JSON Schema expects a string.
    data = json.loads(json.dumps(data, default=_iso))
    validator = jsonschema.Draft202012Validator(
        json_schema("roadbed", reader=True), format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )
    validator.validate(data)
    return data


def _iso(obj: Any) -> str:
    if hasattr(obj, "isoformat"):
        return obj.isoformat().replace("+00:00", "Z")
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")
