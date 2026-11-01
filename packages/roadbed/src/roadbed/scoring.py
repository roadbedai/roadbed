"""Row scoring: weighted exact match of output fields, 0-100."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from roadbed.contracts import Scorers
from roadbed.io import read_yaml


def load_scorers(path: str | Path) -> Scorers:
    return Scorers.model_validate(read_yaml(path))


def score_row(expected: Mapping[str, Any], output: Mapping[str, Any], scorers: Scorers) -> float:
    """100 * sum(weight * [output[f] == expected[f]]) / sum(weight). A missing field scores 0."""
    total = sum(f.weight for f in scorers.fields)
    hit = sum(
        f.weight for f in scorers.fields if f.field in output and f.field in expected and output[f.field] == expected[f.field]
    )
    return 100.0 * hit / total
