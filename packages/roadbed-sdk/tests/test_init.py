"""init() refuses bad configuration with a clear error; ingest without a profile id never leaves the process."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import respx

import roadbed_sdk
from conftest import API, TEST_KEY, FakeIngest
from roadbed_sdk import HttpSink, ProfileRequiredError


@pytest.mark.parametrize("profile", ["", "   ", None])
def test_init_without_profile_id_is_refused(profile: str | None, spool_path: Path) -> None:
    with pytest.raises(ProfileRequiredError, match="profile_id is required"):
        roadbed_sdk.init(API, "test-token", TEST_KEY, profile, spool_path=spool_path, background=False)  # type: ignore[arg-type]
    assert not spool_path.exists()


def test_sink_refuses_an_interaction_without_profile_id_client_side(mock: respx.MockRouter, ingest: FakeIngest,
                                                                     caplog: pytest.LogCaptureFixture) -> None:
    body = {"interaction_id": "ix_1", "model": "m", "version": "v1", "ts": "2026-11-01T10:00:00Z",
            "input_ref": "sha256:" + "0" * 64, "input": {}, "output": {}, "hmac_key_id": "hk_x"}
    res = HttpSink(API, "test-token").write("interaction", [body])
    assert res.rejected == {"ix_1": "profile_required"}
    assert res.accepted == [] and ingest.posts == 0  # nothing was sent
    assert "profile_id missing" in caplog.text


def test_spooled_interaction_without_profile_is_never_sent(ingest: FakeIngest, spool_path: Path) -> None:
    rec = roadbed_sdk.init(API, "test-token", TEST_KEY, "p@v1", spool_path=spool_path, background=False)
    rec.spool.put("interaction", "ix_tampered", '{"interaction_id": "ix_tampered", "model": "m"}')
    counts = roadbed_sdk.flush()
    assert ingest.posts == 0
    assert counts["interaction_rejected"] == 1
    roadbed_sdk.shutdown()


def test_other_config_errors(spool_path: Path) -> None:
    with pytest.raises(roadbed_sdk.ConfigError, match="at least 16 bytes"):
        roadbed_sdk.init(API, "t", b"short", "p@v1", spool_path=spool_path, background=False)
    with pytest.raises(roadbed_sdk.ConfigError, match="plain http"):
        roadbed_sdk.init("http://api.example.eu", "t", TEST_KEY, "p@v1", spool_path=spool_path, background=False)
    with pytest.raises(roadbed_sdk.ConfigError, match="token"):
        roadbed_sdk.init(API, "", TEST_KEY, "p@v1", spool_path=spool_path, background=False)


def test_spool_file_is_private(spool_path: Path) -> None:
    roadbed_sdk.init(API, "t", TEST_KEY, "p@v1", spool_path=spool_path, background=False)
    assert spool_path.stat().st_mode & 0o077 == 0
    assert spool_path.parent.stat().st_mode & 0o077 == 0
    roadbed_sdk.shutdown()


def test_a_pre_existing_spool_file_is_made_private(spool_path: Path) -> None:
    spool_path.parent.mkdir(parents=True, mode=0o700)
    spool_path.touch()
    spool_path.chmod(0o644)
    roadbed_sdk.init(API, "t", TEST_KEY, "p@v1", spool_path=spool_path, background=False)
    assert roadbed_sdk.outcome("ix_1", {"a": 1})
    assert spool_path.stat().st_mode & 0o777 == 0o600
    roadbed_sdk.shutdown()


def test_calls_before_init_do_nothing_and_do_not_raise(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    assert roadbed_sdk.verdict("ix_1", "approve") is None
    assert roadbed_sdk.outcome("ix_1", {"a": 1}) is None
    assert roadbed_sdk.reconcile("missing.csv").error == "not_initialised"
    assert roadbed_sdk.subject_token("x") is None


def test_subject_token_of_a_non_string_returns_none_and_does_not_raise(spool_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    roadbed_sdk.init(API, "t", TEST_KEY, "p@v1", spool_path=spool_path, background=False)
    try:
        assert roadbed_sdk.subject_token(123) is None  # type: ignore[arg-type]
        assert roadbed_sdk.subject_token("claimant-1").startswith("tok_")
        assert "subject_token" in caplog.text
    finally:
        roadbed_sdk.shutdown()


def test_httpx_floor_has_allow_nan_false() -> None:
    # httpx < 0.28 encodes NaN as a bare literal, so the not_json refusal in HttpSink would not exist there.
    from importlib.metadata import requires

    assert "httpx>=0.28" in (requires("roadbed-sdk") or [])


def test_version_is_the_installed_package_version() -> None:
    from importlib.metadata import version

    assert roadbed_sdk.__version__ == version("roadbed-sdk") == "0.1.0"
