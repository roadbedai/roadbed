"""HMAC tokens are stable for the same key and differ across keys. Known answers come from openssl:

    printf 'subject\\0claimant-123' | openssl dgst -sha256 -hmac 'roadbed-test-key-0123456789abcdef'
    (printf 'roadbed-sdk/hmac-key-id/v1\\0'; printf 'roadbed-test-key-0123456789abcdef') | shasum -a 256
"""

from __future__ import annotations

import re

from pydantic import TypeAdapter

from conftest import TEST_KEY
from roadbed.contracts import SubjectToken
from roadbed.labels.guard import contact_shaped, scan_input
from roadbed_sdk._pseudo import Pseudonymiser, Session, derive_key_id, hmac_hex, hmac_token


def test_hmac_known_answer() -> None:
    assert hmac_token(TEST_KEY, "claimant-123") == "tok_81d2fde77376a307ab7dc235a061e649"
    assert derive_key_id(TEST_KEY) == "hk_b372bc33f7565e0c"


def test_hmac_stable_for_the_same_key_and_different_across_keys() -> None:
    other = b"another-tenant-key-0123456789abcdef"
    assert hmac_token(TEST_KEY, "claimant-123") == hmac_token(bytes(TEST_KEY), "claimant-123")
    assert hmac_token(other, "claimant-123") != hmac_token(TEST_KEY, "claimant-123")
    a = Pseudonymiser(TEST_KEY).value({"t": "free text"}, Session(), [])
    b = Pseudonymiser(TEST_KEY).value({"t": "free text"}, Session(), [])
    c = Pseudonymiser(other).value({"t": "free text"}, Session(), [])
    assert a == b != c
    assert derive_key_id(other) != derive_key_id(TEST_KEY)


def test_purposes_are_separated() -> None:
    assert hmac_token(TEST_KEY, "x", "subject") != hmac_token(TEST_KEY, "x", "text")


def test_token_has_no_long_digit_run_so_the_guard_never_reads_it_as_a_phone_number() -> None:
    """32 hex characters hold 9 digits in a row for 14 tokens in 100. The token is then a window of the HMAC hex with no run of 7."""
    values = [f"claimant-{n}" for n in range(20_000)]
    assert any(re.search(r"\d{7}", hmac_hex(TEST_KEY, "text", v)[:32]) for v in values)  # the case exists in this sample
    for v in values:
        for purpose in ("subject", "text", "number"):
            tok = hmac_token(TEST_KEY, v, purpose)
            assert re.fullmatch(r"tok_[0-9a-f]{32}", tok) and not re.search(r"\d{7}", tok), tok
            digest = hmac_hex(TEST_KEY, purpose, v)
            if any(not re.search(r"\d{7}", digest[i:i + 32]) for i in range(33)):
                assert tok[4:] in digest  # a window of the HMAC hex; the HMAC itself is unchanged
            assert contact_shaped(tok) is None and scan_input({"m": f"<TEXT:{tok}>"}) is None
            TypeAdapter(SubjectToken).validate_python(tok)
