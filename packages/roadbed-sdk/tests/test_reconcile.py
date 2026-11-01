"""reconcile(csv) turns a system-of-record export into outcomes; rerunning the same file adds nothing."""

from __future__ import annotations

from pathlib import Path

import pytest

import roadbed_sdk
from conftest import API, TEST_KEY, FakeIngest


def test_reconcile_spools_outcomes_and_is_idempotent(ingest: FakeIngest, spool_path: Path, tmp_path: Path) -> None:
    roadbed_sdk.init(API, "test-token", TEST_KEY, "p@v1", keep=("final_severity", "status"), spool_path=spool_path, background=False)
    ingest.interactions.update({"ix_1": {}, "ix_2": {}})
    csv_path = tmp_path / "sor.csv"
    csv_path.write_text(
        "interaction_id,observed_at,final_severity,status,paid\n"
        "ix_1,2026-11-02,3,closed,true\n"
        "ix_2,2026-11-02T08:30:00+02:00,1,open,false\n"
        ",2026-11-02,1,open,false\n"  # no interaction id: skipped
        "ix_2,2026-11-02T08:30:00,1,open,false\n"  # a time without a zone: skipped, not guessed
    )
    rep = roadbed_sdk.reconcile(csv_path)
    assert (rep.rows, rep.spooled, rep.skipped, rep.error) == (4, 2, 2, None)
    assert roadbed_sdk.reconcile(csv_path).spooled == 2  # same ids: the spool keeps one row each
    roadbed_sdk.flush()
    assert len(ingest.outcomes) == 2
    by_ix = {o["interaction_id"]: o for o in ingest.outcomes.values()}
    assert by_ix["ix_1"]["outcome"] == {"final_severity": 3, "status": "closed", "paid": True}
    assert by_ix["ix_1"]["source"] == "csv"
    assert by_ix["ix_2"]["observed_at"] == "2026-11-02T06:30:00Z"
    roadbed_sdk.shutdown()


def test_reconcile_missing_file_reports_without_raising(spool_path: Path) -> None:
    roadbed_sdk.init(API, "test-token", TEST_KEY, "p@v1", spool_path=spool_path, background=False)
    assert roadbed_sdk.reconcile(spool_path.parent / "nope.csv").error == "FileNotFoundError"
    roadbed_sdk.shutdown()


def test_reconcile_never_raises_on_a_nan_or_inf_cell(ingest: FakeIngest, spool_path: Path, tmp_path: Path) -> None:
    roadbed_sdk.init(API, "test-token", TEST_KEY, "p@v1", keep=("amount",), spool_path=spool_path, background=False)
    ingest.interactions.update({"ix_1": {}, "ix_2": {}})
    csv_path = tmp_path / "sor.csv"
    csv_path.write_text("interaction_id,observed_at,paid,amount\nix_1,2026-11-02,true,nan\nix_2,2026-11-02,false,inf\n")
    rep = roadbed_sdk.reconcile(csv_path)
    assert (rep.rows, rep.spooled, rep.skipped, rep.error) == (2, 2, 0, None)
    roadbed_sdk.flush()
    by_ix = {o["interaction_id"]: o["outcome"] for o in ingest.outcomes.values()}
    assert by_ix == {"ix_1": {"paid": True, "amount": "nan"}, "ix_2": {"paid": False, "amount": "inf"}}
    roadbed_sdk.shutdown()


def test_reconcile_skips_a_row_that_fails(spool_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = roadbed_sdk.init(API, "test-token", TEST_KEY, "p@v1", spool_path=spool_path, background=False)
    real = rec.keyed_id

    def boom(prefix: str, obj: dict[str, object]) -> str:
        if obj["interaction_id"] == "ix_bad":
            raise RuntimeError("bad row")
        return real(prefix, obj)

    monkeypatch.setattr(rec, "keyed_id", boom)
    csv_path = tmp_path / "sor.csv"
    csv_path.write_text("interaction_id,observed_at,paid\nix_bad,2026-11-02,true\nix_2,2026-11-02,false\n")
    rep = roadbed_sdk.reconcile(csv_path)
    assert (rep.rows, rep.spooled, rep.skipped, rep.error) == (2, 1, 1, None)
    roadbed_sdk.shutdown()
