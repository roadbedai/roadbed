"""Live check against the real Hub. Not run in CI: set ROADBED_NETWORK=1 to run it by hand."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from roadbed import watch
from roadbed.contracts import LicensePolicy

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(os.environ.get("ROADBED_NETWORK") != "1", reason="network test; set ROADBED_NETWORK=1"),
]


def test_live_qwen38_27b_is_pinned_and_apache(tmp_path: Path):
    [d] = watch.scan("Qwen/Qwen3.8-27B", tmp_path / "state.json", LicensePolicy(allow=["apache-2.0"]),
                     cache_dir=tmp_path / "cache")
    assert watch.SHA.fullmatch(d.revision) and d.source == "hub"
    assert d.license == "apache-2.0" and d.decision is not None and d.decision.decision == "allow"
