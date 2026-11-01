"""No raw personal data reaches the spool, the wire or the log; only the key id is recorded, never the key."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import openai
import pytest
import respx

import roadbed_sdk
from conftest import API, GATEWAY, TEST_KEY, FakeIngest, chat_completion, spool_bytes
from roadbed_sdk._pseudo import Pseudonymiser, Session, normalise_pii

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
    gw = mock.post(f"{GATEWAY}/chat/completions").mock(return_value=httpx.Response(200, json=chat_completion("{}")))
    client = roadbed_sdk.wrap_openai(oai)
    with roadbed_sdk.capture(interaction_id=f"ix_{EMAIL}") as cap:
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    assert cap.interaction_id and cap.interaction_id.startswith("ix_") and EMAIL not in cap.interaction_id
    # The id is checked before it leaves: the gateway (which logs it) gets the generated id, not the host's.
    assert gw.calls.last.request.headers["x-roadbed-interaction-id"] == cap.interaction_id
    # A host-set header (any case) is replaced by the checked id.
    with roadbed_sdk.capture() as cap2:
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}],
                                       extra_headers={"X-Roadbed-Interaction-Id": f"ix_{EMAIL}"})
    assert gw.calls.last.request.headers.get_list("x-roadbed-interaction-id") == [cap2.interaction_id]
    gateway_headers = b"".join(str(c.request.headers).encode() for c in gw.calls)
    assert roadbed_sdk.verdict("ix_1", "approve", verdict_id=f"vd {NAME}") is None
    assert roadbed_sdk.outcome(f"ix_{SSN}", {"a": 1}) is None
    roadbed_sdk.flush()
    assert _leaks(spool_bytes(spool_path) + b"".join(ingest.raw_bodies) + caplog.text.encode() + gateway_headers) == []
    roadbed_sdk.shutdown()


def test_model_strings_with_personal_data_become_unknown(mock: respx.MockRouter, ingest: FakeIngest, oai: openai.OpenAI,
                                                         spool_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="roadbed_sdk")
    roadbed_sdk.init(API, "test-token", TEST_KEY, "p@v1", spool_path=spool_path, background=False)
    mock.post(f"{GATEWAY}/chat/completions").mock(return_value=httpx.Response(
        200, json=chat_completion("{}", model=f"m-{EMAIL}"), headers={"x-roadbed-model-version": f"v-{PHONE}"}))
    client = roadbed_sdk.wrap_openai(oai)
    with roadbed_sdk.capture(model=f"triage-{EMAIL}"):
        client.chat.completions.create(model=f"alias-{EMAIL}", messages=[{"role": "user", "content": "hi"}])
    roadbed_sdk.flush()
    [body] = ingest.interactions.values()
    assert (body["model"], body["alias"], body["version"]) == ("unknown", "unknown", "unknown")
    assert "model string refused" in caplog.text
    assert _leaks(spool_bytes(spool_path) + b"".join(ingest.raw_bodies) + caplog.text.encode()) == []
    roadbed_sdk.shutdown()


@pytest.mark.parametrize("name", ["claims-triage-v12", "gpt-4o-2024-08-06", "claude-sonnet-4-20250514", "m", "v12"])
def test_real_model_names_are_kept(name: str) -> None:
    assert Pseudonymiser(TEST_KEY).safe_model(name, [])


def test_capitalised_keys_are_tokenised() -> None:
    out = Pseudonymiser(TEST_KEY).value({"Virtanen": 1, "claimNumber": 2, "final_severity": 3}, Session(), [])
    assert set(out) - {"claimNumber", "final_severity"} and "Virtanen" not in out


def test_known_values_match_number_spellings_and_key_spellings() -> None:
    p = Pseudonymiser(TEST_KEY)
    out = p.value({"claim": 123456, "ref": 7.5}, Session(), normalise_pii({"123456.0": "CLAIM_NUMBER", "7.50": "CLAIM_NUMBER"}))
    assert out == {"claim": "<CLAIM_NUMBER_1>", "ref": "<CLAIM_NUMBER_2>"}
    out = p.value({"jane_doe": 1, "Jane-Doe": 2, "janeDoe": 3, "claim_status": 4}, Session(), normalise_pii(["Jane Doe"]))
    assert set(out) - {"claim_status"} and not any("jane" in k.lower() for k in out)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_become_null(bad: float) -> None:
    assert Pseudonymiser(TEST_KEY).value({"x": bad, "y": [bad]}, Session(), []) == {"x": None, "y": [None]}


def test_capture_synthetic_without_init_synthetic_warns_once(mock: respx.MockRouter, ingest: FakeIngest, oai: openai.OpenAI,
                                                             spool_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    roadbed_sdk.init(API, "test-token", TEST_KEY, "p@v1", spool_path=spool_path, background=False)
    mock.post(f"{GATEWAY}/chat/completions").mock(return_value=httpx.Response(200, json=chat_completion("{}")))
    client = roadbed_sdk.wrap_openai(oai)
    for _ in range(2):
        with roadbed_sdk.capture(synthetic=True):
            client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    roadbed_sdk.flush()
    assert sorted(str(b.get("synthetic")) for b in ingest.interactions.values()) == ["None", "True", "True"]
    assert caplog.text.count("synthetic=True in a capture") == 1
    roadbed_sdk.shutdown()


def test_synthetic_flag_is_sticky(mock: respx.MockRouter, ingest: FakeIngest, oai: openai.OpenAI, spool_path: Path) -> None:
    roadbed_sdk.init(API, "test-token", TEST_KEY, "p@v1", synthetic=True, spool_path=spool_path, background=False)
    mock.post(f"{GATEWAY}/chat/completions").mock(return_value=httpx.Response(200, json=chat_completion("{}")))
    client = roadbed_sdk.wrap_openai(oai)
    with roadbed_sdk.capture(synthetic=False):
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    roadbed_sdk.flush()
    assert [b["synthetic"] for b in ingest.interactions.values()] == [True]
    roadbed_sdk.shutdown()


def test_host_named_numeric_values_become_placeholders_everywhere(mock: respx.MockRouter, ingest: FakeIngest, oai: openai.OpenAI,
                                                                  spool_path: Path) -> None:
    # Triage models echo claim numbers as JSON numbers; a host-named value must not pass as a raw int or float.
    roadbed_sdk.init(API, "test-token", TEST_KEY, "p@v1", keep=("claim_number",), spool_path=spool_path, background=False)
    mock.post(f"{GATEWAY}/chat/completions").mock(
        return_value=httpx.Response(200, json=chat_completion('{"claim_number": 123456, "ref": 654321.0}')))
    client = roadbed_sdk.wrap_openai(oai)
    pii = {"123456": "CLAIM_NUMBER", "654321": "CLAIM_NUMBER"}
    with roadbed_sdk.capture(pii=pii) as cap:
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": "claim 123456"}])
    ix = cap.interaction_id
    assert ix
    assert roadbed_sdk.verdict(ix, "fix", {"claim_number": 123456}, reason_code="wrong_claim", pii=pii)
    assert roadbed_sdk.outcome(ix, {"claim_number": 123456, "note": "claim 123456"}, pii=pii)
    roadbed_sdk.flush()
    blob = spool_bytes(spool_path)
    assert b"123456" not in blob and b"654321" not in blob
    roadbed_sdk.shutdown()


@pytest.mark.parametrize("value", [123456, 123456.0, -1])
def test_numeric_known_answer(value: float) -> None:
    out = Pseudonymiser(TEST_KEY).value({"n": value}, Session(), [("123456", "CLAIM_NUMBER")])["n"]
    assert out == (-1 if value == -1 else "<CLAIM_NUMBER_1>")


def test_kept_values_and_slice_labels_must_be_lowercase_labels() -> None:
    p = Pseudonymiser(TEST_KEY, keep=("route",))
    out = p.value({"route": "Virtanen", "role": "Jane.Doe", "type": "text"}, Session(), [])
    assert out["route"].startswith("<TEXT:tok_") and out["role"].startswith("<TEXT:tok_")
    assert out["type"] == "text"
    assert p.labels({"claimant": "Virtanen", "line": "auto"}, Session(), [])["claimant"].startswith("<TEXT:tok_")


def test_a_date_glued_to_letters_is_found() -> None:
    p = Pseudonymiser(TEST_KEY)
    assert p._patterns("janedoe1985-03-12", Session()) == "janedoe<DATE_TIME_1>"
    assert not p.safe_id("ix_janedoe1985-03-12")


def test_redact_mode_overlapping_spans_take_the_longest() -> None:
    class OverlapNer:  # stub: two spans at one start, the shorter first
        def find(self, text: str) -> list[tuple[int, int, str]]:
            i = text.find(NAME)
            return [] if i < 0 else [(i, i + 4, "PERSON"), (i, i + len(NAME), "PERSON")]

    p = Pseudonymiser(TEST_KEY, text="redact", redactor=OverlapNer())
    assert p.value({"a": f"{NAME} fell"}, Session(), []) == {"a": "<PERSON_1> fell"}


def _shingles(obj: object, k: int = 3) -> set[str]:
    s = json.dumps(obj, sort_keys=True)
    return {s[i:i + k] for i in range(len(s) - k + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b)


def test_near_duplicate_detection_needs_redact_mode() -> None:
    """Tokenize mode keeps no text, so a 1-character variant of a holdout input looks as far as an unrelated row.
    Redact mode keeps the text and the near-copy stays near. The README says which mode training-bound text needs."""

    class NoNer:  # stub: finds nothing, so only the text mode differs
        def find(self, text: str) -> list[tuple[int, int, str]]:
            return []

    holdout = "Claimant fell at the store on a wet floor and hurt the left knee."
    texts = (holdout, holdout[:-1], "Hail damaged the roof of the insured's garage during the storm.")
    for mode, redactor in (("tokenize", None), ("redact", NoNer())):
        p = Pseudonymiser(TEST_KEY, text=mode, redactor=redactor)  # type: ignore[arg-type]
        rows = [_shingles(p.value({"messages": [{"role": "user", "content": t}]}, Session(), [])) for t in texts]
        near, unrelated = _jaccard(rows[0], rows[1]), _jaccard(rows[0], rows[2])
        if mode == "tokenize":
            assert near < 0.8 and abs(near - unrelated) < 0.1  # the known limit, stated in the README
        else:
            assert near >= 0.8 > unrelated
