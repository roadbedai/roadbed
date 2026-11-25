"""roadbed sample: the sample ships inside the wheel and is copied out of it."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from roadbed.samples import copy_sample, list_samples

PKG = Path(str(resources.files("roadbed.samples")))


def test_list_samples_names_acme_2k() -> None:
    assert list_samples() == ["acme-2k"]


def test_copy_sample_equals_the_package_data_byte_for_byte(tmp_path: Path) -> None:
    dest = tmp_path / "events"
    written = copy_sample("acme-2k", dest)
    src = {p.name: p.read_bytes() for p in (PKG / "acme-2k").iterdir() if p.is_file()}
    assert src and {p.name: p.read_bytes() for p in dest.iterdir()} == src
    assert sorted(written) == sorted(src)
    assert {"interactions.jsonl.gz", "verdicts.jsonl.gz", "outcomes.jsonl.gz", "slices.yaml", "output.schema.json",
            "SYNTHETIC.md", "README.md", "stand_in_predictions.py"} <= set(src)
