"""seal_holdout: reproduces the fixture seal byte for byte, writes once, and keeps raw input out."""

from __future__ import annotations

import json

import pytest

from roadbed.asset import ObjectStore, SealError, seal_holdout
from test_asset_support import EXAMPLES, builder

STRAT = {"keys": ["line", "state", "channel"], "method": "proportional", "fraction": 0.4}


def _source():
    b = builder()
    hv = [b.verdict_row("h", i, "bodily_injury" if i < 30 else b.OTHER_LINES[i % 2]) for i in range(60)]
    rows = b.holdout_rows(hv, "clm")
    inputs = {r["input_ref"]: b.narrative("h", i) for i, r in enumerate(hv)}
    return rows, inputs


def _seal(root, rows, inputs, store, **kw):
    args = dict(sealed_by="vik", sealed_at="2026-11-01T09:02:00Z", audit_seq=1204, seed=20261101, synthetic=True,
                stratification=STRAT, snapshot="S-2026-10-31", inputs=inputs, store=store)
    args.update(kw)
    return seal_holdout(root, "H-2026-11-01", rows, **args)


def test_reproduces_fixture_seal(tmp_path):
    rows, inputs = _source()
    store = ObjectStore(tmp_path / "bucket")
    seal = _seal(tmp_path / "asset", rows, inputs, store)
    d = "data/holdout/H-2026-11-01"
    for name in ("SEAL.json", "holdout.jsonl"):
        assert (tmp_path / "asset" / d / name).read_bytes() == (EXAMPLES / "pass" / d / name).read_bytes(), name
    assert store.has(seal["minhash_oid"])  # the MinHash object is in the bucket, under the oid the fixture names


def test_seal_is_written_once(tmp_path):
    rows, inputs = _source()
    store = ObjectStore(tmp_path / "bucket")
    _seal(tmp_path, rows, inputs, store)
    before = (tmp_path / "data/holdout/H-2026-11-01/SEAL.json").read_bytes()
    with pytest.raises(SealError, match="written once"):
        _seal(tmp_path, rows, inputs, store, audit_seq=1)
    assert (tmp_path / "data/holdout/H-2026-11-01/SEAL.json").read_bytes() == before


def test_raw_input_field_is_refused(tmp_path):
    rows, inputs = _source()
    rows[3] = {**rows[3], "input": "Jane Roe, 12 Elm St, rear collision"}
    with pytest.raises(SealError, match="holdout row 4"):
        _seal(tmp_path, rows, inputs, ObjectStore(tmp_path / "b"))
    assert not (tmp_path / "data").exists()


def test_input_must_match_its_address(tmp_path):
    rows, inputs = _source()
    inputs[rows[0]["input_ref"]] = "some other text"
    with pytest.raises(SealError, match="does not match its input_ref"):
        _seal(tmp_path, rows, inputs, ObjectStore(tmp_path / "b"))


@pytest.mark.parametrize("field", ["row_hash", "input_ref", "id"])
def test_duplicate_rows_refused(tmp_path, field):
    rows, inputs = _source()
    rows[1] = {**rows[1], field: rows[0][field]}
    with pytest.raises(SealError):
        _seal(tmp_path, rows, inputs, ObjectStore(tmp_path / "b"))


def test_sealer_is_a_user_id(tmp_path):
    rows, inputs = _source()
    with pytest.raises(SealError, match="sealed_by"):
        _seal(tmp_path, rows, inputs, ObjectStore(tmp_path / "b"), sealed_by="vik@acme.example")


def test_full_seal_needs_every_part(tmp_path):
    rows, inputs = _source()
    with pytest.raises(SealError, match="full seal"):
        _seal(tmp_path, rows, inputs, None)


def test_day0_seal(tmp_path):
    rows, _ = _source()
    seal = seal_holdout(tmp_path, "H-2026-11-01", rows, sealed_by="vik", sealed_at="2026-11-01T09:02:00Z", audit_seq=5,
                        seed=1, synthetic=False)
    assert "merkle_root" not in seal and "minhash_oid" not in seal
    assert json.loads((tmp_path / "data/holdout/H-2026-11-01/SEAL.json").read_text()) == seal
    assert not (tmp_path / "data/holdout/H-2026-11-01/SYNTHETIC.md").exists()


def test_local_seal_has_provenance_and_no_audit_seq(tmp_path):
    from roadbed.asset import validate_file

    rows, inputs = _source()
    seal = _seal(tmp_path, rows, inputs, ObjectStore(tmp_path / "b"), audit_seq=None, provenance="local")
    assert seal["provenance"] == "local" and "audit_seq" not in seal
    assert json.loads((tmp_path / "data/holdout/H-2026-11-01/SEAL.json").read_text()) == seal
    validate_file(tmp_path, "data/holdout/H-2026-11-01/SEAL.json", strict=True)


def test_local_seal_refuses_an_audit_seq(tmp_path):
    rows, inputs = _source()
    with pytest.raises(SealError, match="SEAL.json"):
        _seal(tmp_path, rows, inputs, ObjectStore(tmp_path / "b"), provenance="local")
    assert not (tmp_path / "data").exists()


def test_local_day0_seal_is_refused(tmp_path):
    rows, _ = _source()
    with pytest.raises(SealError, match="SEAL.json"):
        seal_holdout(tmp_path, "H-2026-11-01", rows, sealed_by="vik", sealed_at="2026-11-01T09:02:00Z", audit_seq=None,
                     seed=1, synthetic=False, provenance="local")
    assert not (tmp_path / "data").exists()


def test_control_plane_seal_needs_an_audit_seq(tmp_path):
    rows, inputs = _source()
    with pytest.raises(SealError, match="audit_seq"):
        _seal(tmp_path, rows, inputs, ObjectStore(tmp_path / "b"), audit_seq=None)


def test_seal_line_names_a_local_seal():
    from roadbed.cli.remote import seal_line
    from roadbed.contracts import Seal

    doc = json.loads((EXAMPLES / "pass/data/holdout/H-2026-11-01/SEAL.json").read_text())
    assert "audit seq 1,204" in seal_line(Seal.model_validate(doc))
    local = {**{k: v for k, v in doc.items() if k != "audit_seq"}, "provenance": "local"}
    assert "local seal, no audit seq" in seal_line(Seal.model_validate(local))


def test_a_symlinked_holdout_dir_is_refused_and_nothing_is_written_through_it(tmp_path):
    rows, inputs = _source()
    victim = tmp_path / "victim"
    victim.mkdir()
    (tmp_path / "asset/data/holdout").mkdir(parents=True)
    (tmp_path / "asset/data/holdout/H-2026-11-01").symlink_to(victim)
    with pytest.raises(SealError, match="symlink"):
        _seal(tmp_path / "asset", rows, inputs, ObjectStore(tmp_path / "b"))
    assert list(victim.iterdir()) == []
