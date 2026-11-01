"""HMAC tokens are stable for the same key and differ across keys. Known answers come from openssl:

    printf 'subject\\0claimant-123' | openssl dgst -sha256 -hmac 'roadbed-test-key-0123456789abcdef'
    (printf 'roadbed-sdk/hmac-key-id/v1\\0'; printf 'roadbed-test-key-0123456789abcdef') | shasum -a 256
"""

from __future__ import annotations

from conftest import TEST_KEY
from roadbed_sdk._pseudo import Pseudonymiser, Session, derive_key_id, hmac_token


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
