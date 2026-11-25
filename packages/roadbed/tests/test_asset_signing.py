"""Ed25519: the conformance vector, the fixture signatures, and what must not verify."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from roadbed.asset import SignatureError, keyid, sign_file, verify_signature, write_signature
from roadbed.asset.signing import public_pem
from test_asset_support import CONFORMANCE, EXAMPLES, copy

VEC = json.loads((CONFORMANCE / "vectors/ed25519.json").read_text())
RECEIPT = dict(signed="rebases/13/receipt.json", key_path="policy/keys/receipt.pub", payload="jcs")
EVIDENCE = dict(signed="rebases/13/evidence/MANIFEST.sha256", key_path="policy/keys/evidence.pub", payload="raw")


def test_ed25519_vector():
    key = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(VEC["seed_string"].encode()).digest())
    assert key.public_key().public_bytes_raw().hex() == VEC["public_raw_hex"]
    assert keyid(key.public_key()) == VEC["keyid"]
    sig = key.sign(VEC["message_utf8"].encode())  # ed25519 is deterministic: a known answer
    assert base64.b64encode(sig).decode() == VEC["sig_b64"]
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(VEC["public_raw_hex"])).verify(sig, VEC["message_utf8"].encode())


def test_fixture_signatures_verify():
    root = EXAMPLES / "pass"
    assert verify_signature(root, "rebases/13/receipt.sig", **RECEIPT) == VEC["keyid"]
    verify_signature(root, "versions/v12/receipt.sig", signed="versions/v12/receipt.json",
                     key_path="policy/keys/receipt.pub", payload="jcs")
    verify_signature(root, "rebases/13/evidence/MANIFEST.sig", **EVIDENCE)


def test_jcs_payload_ignores_formatting(tmp_path):
    root = copy("pass", tmp_path / "a")
    f = root / "rebases/13/receipt.json"
    f.write_text(json.dumps(json.loads(f.read_text()), separators=(",", ":"), sort_keys=True))
    verify_signature(root, "rebases/13/receipt.sig", **RECEIPT)


def test_tampered_receipt_fails(tmp_path):
    root = copy("pass", tmp_path / "a")
    f = root / "rebases/13/receipt.json"
    f.write_text(f.read_text().replace('"audit_seq": 1377', '"audit_seq": 1378'))
    with pytest.raises(SignatureError) as e:
        verify_signature(root, "rebases/13/receipt.sig", **RECEIPT)
    assert e.value.code == "V-SIG-INVALID"


def test_tampered_raw_manifest_fails(tmp_path):
    root = copy("pass", tmp_path / "a")
    f = root / "rebases/13/evidence/MANIFEST.sha256"
    f.write_bytes(f.read_bytes() + b"\n")  # one byte more: raw payload
    with pytest.raises(SignatureError, match="does not verify"):
        verify_signature(root, "rebases/13/evidence/MANIFEST.sig", **EVIDENCE)


@pytest.mark.parametrize("field,value", [("signed", "versions/v12/receipt.json"), ("key", "policy/keys/evidence.pub"),
                                          ("payload", "raw")])
def test_signature_must_cover_what_the_caller_expects(tmp_path, field, value):
    root = copy("pass", tmp_path / "a")
    f = root / "rebases/13/receipt.sig"
    doc = json.loads(f.read_text())
    doc[field] = value
    f.write_text(json.dumps(doc))
    with pytest.raises(SignatureError) as e:
        verify_signature(root, "rebases/13/receipt.sig", **RECEIPT)
    assert e.value.code == "V-SIG-INVALID"


def test_swapped_key_file_changes_keyid(tmp_path):
    """Replacing receipt.pub with an attacker key and re-signing passes the signature itself, but the key id changes;
    pinning the expected key id catches it."""
    root = copy("pass", tmp_path / "a")
    evil = Ed25519PrivateKey.from_private_bytes(bytes(32))
    (root / "policy/keys/receipt.pub").write_bytes(public_pem(evil))
    write_signature(root / "rebases/13/receipt.sig", sign_file(evil, root, "rebases/13/receipt.json", "policy/keys/receipt.pub", "jcs"))
    assert verify_signature(root, "rebases/13/receipt.sig", **RECEIPT) == keyid(evil.public_key())
    with pytest.raises(SignatureError) as e:
        verify_signature(root, "rebases/13/receipt.sig", **RECEIPT, trusted_keyids={VEC["keyid"]})
    assert e.value.code == "V-SIG-UNTRUSTED"


def test_key_outside_policy_keys_is_refused(tmp_path):
    root = copy("pass", tmp_path / "a")
    with pytest.raises(SignatureError):
        verify_signature(root, "rebases/13/receipt.sig", signed="rebases/13/receipt.json", key_path="../pass/policy/keys/receipt.pub",
                         payload="jcs")


def test_missing_signature(tmp_path):
    root = copy("pass", tmp_path / "a")
    (root / "rebases/13/receipt.sig").unlink()
    with pytest.raises(SignatureError) as e:
        verify_signature(root, "rebases/13/receipt.sig", **RECEIPT)
    assert e.value.code == "V-SIG-MISSING"


def test_sign_round_trip(tmp_path):
    key = Ed25519PrivateKey.generate()
    (tmp_path / "policy/keys").mkdir(parents=True)
    (tmp_path / "policy/keys/receipt.pub").write_bytes(public_pem(key))
    (tmp_path / "r.json").write_text('{"b": 1, "a": [1, 2]}')
    doc = sign_file(key, tmp_path, "r.json", "policy/keys/receipt.pub", "jcs")
    write_signature(tmp_path / "r.sig", doc)
    assert verify_signature(tmp_path, "r.sig", signed="r.json", key_path="policy/keys/receipt.pub", payload="jcs") == doc["keyid"]
