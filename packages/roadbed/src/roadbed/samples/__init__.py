"""Sample event streams that ship inside the wheel. Every row is synthetic."""

from __future__ import annotations

from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path


def _root() -> Traversable:
    return resources.files(__name__)


def list_samples() -> list[str]:
    return sorted(p.name for p in _root().iterdir() if p.is_dir() and not p.name.startswith("__"))


def copy_sample(name: str, dest: str | Path) -> list[str]:
    """Copy the sample `name` into the new directory `dest`. Returns the file names written."""
    src = _root().joinpath(name)
    if name not in list_samples():
        raise ValueError(f"no sample named {name!r}; samples: {', '.join(list_samples())}")
    out = Path(dest)
    out.mkdir(parents=True)
    written = []
    for p in sorted(src.iterdir(), key=lambda x: x.name):
        if p.is_file():
            (out / p.name).write_bytes(p.read_bytes())
            written.append(p.name)
    return written
