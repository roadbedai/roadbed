"""Row scoring from evals/scorers.yaml: weighted exact match of output fields, 0-100."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from roadbed.contracts import Scorers
from roadbed.io import read_yaml


def load_scorers(path: str | Path) -> Scorers:
    return Scorers.model_validate(read_yaml(path))


def same_json(a: Any, b: Any) -> bool:
    """JSON equality: like ==, except that a boolean only equals a boolean (Python has True == 1)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return a.keys() == b.keys() and all(same_json(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same_json(x, y) for x, y in zip(a, b))
    return bool(a == b)


def score_row(expected: Mapping[str, Any], output: Mapping[str, Any], scorers: Scorers) -> float:
    """100 * sum(weight * [output[f] == expected[f]]) / sum(weight), JSON equality. A missing field scores 0."""
    total = sum(f.weight for f in scorers.fields)
    hit = sum(
        f.weight for f in scorers.fields
        if f.field in output and f.field in expected and same_json(output[f.field], expected[f.field])
    )
    return 100.0 * hit / total
