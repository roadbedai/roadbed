"""No raw personal data reaches the spool, the wire or the log; only the key id is recorded, never the key."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import openai
import pytest
import respx

import roadbed_sdk
from conftest import API, GATEWAY, TEST_KEY, FakeIngest, chat_completion, spool_bytes
from roadbed_sdk._pseudo import Pseudonymiser, Session

NAME, EMAIL, PHONE, IBAN = "Jane Doe", "jane.doe@example.com", "+358 40 123 4567", "FI21 1234 5600 0007 85"
UNLISTED = "Mikko Virtanen"  # a name the host did not list: free text must still not carry it
SUBJECT = "claimant-778899"
SSN = "123-45-6789"
RAW = [NAME, EMAIL, PHONE, IBAN, UNLISTED, SUBJECT, SSN, "Virtanen", "jane.doe", "4012888888881881"]


def _leaks(blob: bytes) -> list[str]:
    low = blob.lower()
    found = [s for s in RAW if s.lower().encode() in low or s.replace(" ", "").lower().encode() in low]
    if TEST_KEY in blob:
        found.append("tenant key")
    return found


def test_no_raw_pii_in_spool_wire_or_log(mock: respx.MockRouter, ingest: FakeIngest, oai: openai.OpenAI, spool_path: Path,
                                         tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="roadbed_sdk")
    roadbed_sdk.init(API, "test-token", TEST_KEY, "insurer-default@v1", keep=("route", "severity", "claimant", "note"),
                     spool_path=spool_path, background=False)
    echo = f'{{"severity": 2, "route": "{EMAIL}", "claimant": "{NAME}", "note": "{UNLISTED}", "phone": 358401234567, "card": "4012888888881881"}}'
    mock.post(f"{GATEWAY}/chat/completions").mock(return_value=httpx.Response(200, json=chat_completion(echo)))
    client = roadbed_sdk.wrap_openai(oai)
    ingest.down = True
    msgs = [{"role": "user", "content": f"{NAME} <{EMAIL}> {PHONE}, IBAN {IBAN}, SSN {SSN}. Witness: {UNLISTED}."},
            {"role": "user", "content": [{"type": "text", "text": f"Call {PHONE}"}]}]
    with roadbed_sdk.capture(subject=SUBJECT, pii=[NAME], slices={"line": "auto", "agent": EMAIL}) as cap:
        client.chat.completions.create(model="m", messages=msgs)
    ix = cap.interaction_id
    assert ix
    roadbed_sdk.verdict(ix, "fix", {"severity": 3, "route": "fast_track", "comment": f"{UNLISTED} called from {PHONE}"},
                        reason_code="underestimated_injury", pii=[NAME])
    roadbed_sdk.outcome(ix, {"paid_to": NAME, "amount": 1200, NAME: "x"}, pii=[NAME], observed_at="2026-11-01T10:00:00Z")
    csv_path = tmp_path / "outcomes.csv"
    csv_path.write_text(f"interaction_id,observed_at,payee,email\n{ix},2026-11-02,{UNLISTED},{EMAIL}\n")
    assert roadbed_sdk.reconcile(csv_path).spooled == 1

    counts = roadbed_sdk.flush()  # control plane down: everything stays in the spool
    assert (counts["interaction"], counts["verdict"], counts["outcome"]) == (1, 1, 2)
    assert _leaks(spool_bytes(spool_path)) == []

    ingest.down = False
    roadbed_sdk.flush()
    assert len(ingest.interactions) == 1 and len(ingest.verdicts) == 1 and len(ingest.outcomes) == 2
    assert _leaks(b"".join(ingest.raw_bodies)) == []
    assert _leaks(caplog.text.encode()) == []

    body = ingest.interactions[ix]
    assert body["output"]["severity"] == 2
    assert body["output"]["route"] == "<EMAIL_ADDRESS_1>"  # a kept field is still pattern-cleaned
    assert body["output"]["claimant"] == "<PERSON_1>"  # a host-named value, same number as in the input
    assert body["output"]["note"].startswith("<TEXT:tok_")  # kept, but not an identifier: tokenised
    assert body["output"]["phone"].startswith("<NUMBER:tok_")
    assert body["slices"] == {"line": "auto", "agent": "<EMAIL_ADDRESS_1>"}
    roadbed_sdk.shutdown()


def test_redact_mode_uses_the_redactor_and_numbers_placeholders_per_interaction() -> None:
    class StubNer:  # stands in for Presidio: finds one fixed name. Tests the plumbing, not the NER.
        def find(self, text: str) -> list[tuple[int, int, str]]:
            i = text.find(UNLISTED)
            return [] if i < 0 else [(i, i + len(UNLISTED), "PERSON")]

    p = Pseudonymiser(TEST_KEY, text="redact", redactor=StubNer())
    s = Session()
    out = p.value({"a": f"{NAME} met {UNLISTED} and {NAME}; mail {EMAIL}", "b": f"{UNLISTED} again"}, s, [(NAME, "PERSON")])
    assert out == {"a": "<PERSON_1> met <PERSON_2> and <PERSON_1>; mail <EMAIL_ADDRESS_1>", "b": "<PERSON_2> again"}


def test_presidio_redactor_when_installed() -> None:
    pytest.importorskip("presidio_analyzer")
    from roadbed_sdk._redact import PresidioRedactor

    try:
        red = PresidioRedactor()
    except Exception as e:  # pragma: no cover - needs a spaCy model
        pytest.skip(f"Presidio has no language model: {type(e).__name__}")
    p = Pseudonymiser(TEST_KEY, text="redact", redactor=red)
    out = p.string(f"Claim filed by {UNLISTED} in Helsinki.", Session(), [])
    assert UNLISTED not in out and "Virtanen" not in out


def test_redact_mode_without_presidio_is_a_clear_config_error(spool_path: Path) -> None:
    try:
        import presidio_analyzer  # noqa: F401
    except ImportError:
        with pytest.raises(roadbed_sdk.ConfigError, match=r"roadbed-sdk\[redact\]"):
            roadbed_sdk.init(API, "t", TEST_KEY, "p@v1", text="redact", spool_path=spool_path, background=False)
    else:  # pragma: no cover
        pytest.skip("Presidio installed")


@pytest.mark.parametrize("value", ["ok_label", "fast_track", "v12", "bodily_injury"])
def test_identifier_labels_under_keep_are_kept(value: str) -> None:
    p = Pseudonymiser(TEST_KEY, keep=("route",))
    assert p.value({"route": value}, Session(), [])["route"] == value


@pytest.mark.parametrize("value", ["S-2026-11-01", "Jane Doe", "x" * 65, f"to:{EMAIL}"])
def test_kept_labels_with_personal_parts_or_spaces_are_tokenised(value: str) -> None:
    p = Pseudonymiser(TEST_KEY, keep=("route",))
    assert p.value({"route": value}, Session(), [])["route"].startswith("<TEXT:tok_")


def test_ids_with_personal_data_are_not_used(mock: respx.MockRouter, ingest: FakeIngest, oai: openai.OpenAI, spool_path: Path,
                                            caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="roadbed_sdk")
    roadbed_sdk.init(API, "test-token", TEST_KEY, "p@v1", spool_path=spool_path, background=False)
    mock.post(f"{GATEWAY}/chat/completions").mock(return_value=httpx.Response(200, json=chat_completion("{}")))
    client = roadbed_sdk.wrap_openai(oai)
    with roadbed_sdk.capture(interaction_id=f"ix_{EMAIL}") as cap:
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    assert cap.interaction_id and cap.interaction_id.startswith("ix_") and EMAIL not in cap.interaction_id
    assert roadbed_sdk.verdict("ix_1", "approve", verdict_id=f"vd {NAME}") is None
    assert roadbed_sdk.outcome(f"ix_{SSN}", {"a": 1}) is None
    roadbed_sdk.flush()
    assert _leaks(spool_bytes(spool_path) + b"".join(ingest.raw_bodies) + caplog.text.encode()) == []
    roadbed_sdk.shutdown()


def test_capitalised_keys_are_tokenised() -> None:
    out = Pseudonymiser(TEST_KEY).value({"Virtanen": 1, "claimNumber": 2, "final_severity": 3}, Session(), [])
    assert set(out) - {"claimNumber", "final_severity"} and "Virtanen" not in out


def test_synthetic_flag_is_sticky(mock: respx.MockRouter, ingest: FakeIngest, oai: openai.OpenAI, spool_path: Path) -> None:
    roadbed_sdk.init(API, "test-token", TEST_KEY, "p@v1", synthetic=True, spool_path=spool_path, background=False)
    mock.post(f"{GATEWAY}/chat/completions").mock(return_value=httpx.Response(200, json=chat_completion("{}")))
    client = roadbed_sdk.wrap_openai(oai)
    with roadbed_sdk.capture(synthetic=False):
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    roadbed_sdk.flush()
    assert [b["synthetic"] for b in ingest.interactions.values()] == [True]
    roadbed_sdk.shutdown()
