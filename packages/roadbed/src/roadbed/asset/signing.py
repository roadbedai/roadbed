"""ed25519 signatures of receipts and evidence manifests (SPEC.md, Signatures and receipts).

A signature file (`*.sig`) names its key (`policy/keys/<name>.pub`, SubjectPublicKeyInfo PEM), the signed file and
the payload form: `jcs` signs the RFC 8785 bytes of the signed JSON file (receipts), `raw` signs the file bytes
(manifests). The key id is sha256 of the raw 32-byte public key.

Trust: keys live in the asset, so a signature proves only that the holder of that key signed. Pass
`trusted_keyids` to pin the keys you expect (for example from your own records); verify reports the key ids it used.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Collection
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from roadbed.asset.files import AssetValidationError, validate
from roadbed.asset.hashing import jcs, sha256_bytes

Payload = Literal["jcs", "raw"]
KEYS_DIR = "policy/keys/"


class SignatureError(Exception):
    """A signature is missing, malformed, made by an unexpected key, or does not verify."""

    def __init__(self, code: str, msg: str) -> None:
        super().__init__(msg)
        self.code = code


def raw_public(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def keyid(key: Ed25519PublicKey) -> str:
    """Lowercase hex sha256 of the raw 32-byte public key."""
    return sha256_bytes(raw_public(key))


def public_pem(key: Ed25519PrivateKey | Ed25519PublicKey) -> bytes:
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def load_public(path: Path) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise SignatureError("V-SIG-INVALID", f"{path.name} is not an ed25519 public key")
    return key


def message(root: Path, signed: str, payload: Payload) -> bytes:
    """The bytes a signature covers."""
    data = (root / signed).read_bytes()
    if payload == "jcs":
        return jcs(json.loads(data))
    return data


def sign_file(key: Ed25519PrivateKey, root: str | Path, signed: str, key_path: str, payload: Payload) -> dict[str, Any]:
    """Sign <root>/<signed> and return the signature document (schema `signature`). The caller writes it."""
    r = Path(root)
    doc = {
        "alg": "ed25519",
        "keyid": keyid(key.public_key()),
        "key": key_path,
        "signed": signed,
        "payload": payload,
        "sig": base64.b64encode(key.sign(message(r, signed, payload))).decode(),
    }
    validate(doc, "signature", strict=True)
    return doc


def write_signature(path: str | Path, doc: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def _inside(root: Path, rel: str) -> bool:
    if rel.startswith("/") or "\\" in rel or any(part in ("", ".", "..") for part in rel.split("/")):
        return False
    p = root / rel
    return not p.is_symlink() and p.resolve().is_relative_to(root.resolve())


def verify_signature(
    root: str | Path,
    sig_rel: str,
    *,
    signed: str,
    key_path: str,
    payload: Payload,
    trusted_keyids: Collection[str] | None = None,
) -> str:
    """Check the signature file <root>/<sig_rel> over <signed>, made with <key_path> in form <payload>.

    The caller says what the signature must cover, so a valid signature over another file or with another key
    does not pass. Returns the key id. Raises SignatureError (code V-SIG-MISSING, V-SIG-INVALID or V-SIG-UNTRUSTED).
    """
    r = Path(root)
    sp = r / sig_rel
    if not sp.is_file():
        raise SignatureError("V-SIG-MISSING", f"{sig_rel} not found")
    try:
        doc = json.loads(sp.read_text(encoding="utf-8"))
        validate(doc, "signature")
    except (ValueError, AssetValidationError) as e:
        raise SignatureError("V-SIG-INVALID", f"{sig_rel}: {e}") from None
    for field, want in (("signed", signed), ("key", key_path), ("payload", payload)):
        if doc[field] != want:
            raise SignatureError("V-SIG-INVALID", f"{sig_rel}: {field} is {doc[field]!r}, expected {want!r}")
    if not key_path.startswith(KEYS_DIR) or not _inside(r, key_path) or not _inside(r, signed):
        raise SignatureError("V-SIG-INVALID", f"{sig_rel}: key and signed file must be inside the asset, key under {KEYS_DIR}")
    kp = r / key_path
    if not kp.is_file():
        raise SignatureError("V-SIG-MISSING", f"{sig_rel}: key {key_path} not found")
    try:
        pub = load_public(kp)
    except ValueError as e:
        raise SignatureError("V-SIG-INVALID", f"{key_path}: {e}") from None
    kid = keyid(pub)
    if doc["keyid"] != kid:
        raise SignatureError("V-SIG-INVALID", f"{sig_rel}: keyid does not match {key_path}")
    if trusted_keyids is not None and kid not in trusted_keyids:
        raise SignatureError("V-SIG-UNTRUSTED", f"{sig_rel}: key {kid} is not a trusted key")
    if not (r / signed).is_file():
        raise SignatureError("V-SIG-INVALID", f"{sig_rel}: signed file {signed} not found")
    try:
        sig = base64.b64decode(doc["sig"], validate=True)
        msg = message(r, signed, payload)
        pub.verify(sig, msg)
    except (binascii.Error, ValueError, InvalidSignature):
        raise SignatureError("V-SIG-INVALID", f"{sig_rel}: signature does not verify over {signed}") from None
    return kid
