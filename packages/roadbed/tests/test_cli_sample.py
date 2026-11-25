"""roadbed sample: the sample ships inside the wheel and is copied out of it."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from typer.testing import CliRunner

from roadbed.cli.evals import read_events
from roadbed.cli.main import app
from roadbed.labels import resolve
from roadbed.samples import copy_sample, list_samples

PKG = Path(str(resources.files("roadbed.samples")))
cli = CliRunner()


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


# ---------------------------------------------------------------- the command


def test_command_copies_the_sample_and_names_the_next_step(tmp_path: Path) -> None:
    dest = tmp_path / "events"
    r = cli.invoke(app, ["sample", "acme-2k", str(dest)])
    assert r.exit_code == 0, r.output
    for p in (PKG / "acme-2k").iterdir():
        assert (dest / p.name).read_bytes() == p.read_bytes()
    assert "interactions.jsonl.gz" in r.output
    assert "roadbed new" in r.output and "slices.yaml" in r.output and "output.schema.json" in r.output


def test_copied_events_resolve(tmp_path: Path) -> None:
    dest = tmp_path / "events"
    assert cli.invoke(app, ["sample", "acme-2k", str(dest)]).exit_code == 0
    ints, vds, ocs = (read_events(dest, n) for n in ("interactions", "verdicts", "outcomes"))
    assert len(ints) == 2000 and len(vds) == 2000
    assert len(resolve(ints, vds, ocs, as_of="2026-11-01T09:00:00Z")) > 0


def test_an_existing_dir_is_refused(tmp_path: Path) -> None:
    dest = tmp_path / "events"
    dest.mkdir()
    r = cli.invoke(app, ["sample", "acme-2k", str(dest)])
    assert r.exit_code == 2 and "exists" in r.output
    assert list(dest.iterdir()) == []


def test_a_dir_inside_an_asset_is_refused(tmp_path: Path) -> None:
    (tmp_path / "roadbed.yaml").write_text("spec: roadbed/v0.1\n", encoding="utf-8")
    r = cli.invoke(app, ["sample", "acme-2k", str(tmp_path / "evals" / "events")])
    assert r.exit_code == 2 and "asset" in r.output
    assert not (tmp_path / "evals").exists()


def test_an_unknown_sample_is_refused(tmp_path: Path) -> None:
    r = cli.invoke(app, ["sample", "nope", str(tmp_path / "x")])
    assert r.exit_code == 2 and "acme-2k" in r.output and not (tmp_path / "x").exists()


def test_list_prints_the_names() -> None:
    r = cli.invoke(app, ["sample", "--list"])
    assert r.exit_code == 0 and r.output.split() == ["acme-2k"]
    assert json.loads(cli.invoke(app, ["sample", "--list", "--json"]).output)["samples"] == ["acme-2k"]


def test_a_dangling_symlink_is_refused_and_not_followed(tmp_path: Path) -> None:
    (tmp_path / "dangling").symlink_to(tmp_path / "elsewhere" / "made_by_sample")
    r = cli.invoke(app, ["sample", "acme-2k", str(tmp_path / "dangling")])
    assert r.exit_code == 2 and "exists" in r.output, r.output
    assert not (tmp_path / "elsewhere").exists()


def test_the_next_hint_quotes_the_path(tmp_path: Path) -> None:
    dest = tmp_path / "x;touch PWN" / "my events"
    r = cli.invoke(app, ["sample", "acme-2k", str(dest), "--json"])
    assert r.exit_code == 0, r.output
    nxt = json.loads(r.output)["next"]
    assert f"--slices '{dest}/slices.yaml' --output-schema '{dest}/output.schema.json'" in nxt
