"""Offline library over the asset tree: load, validate, hash, lock, seal, sign and verify.

    from roadbed.asset import load, validate, row_hash, merkle_root, seal_holdout, verify

Every rule here is SPEC.md v0.1; nothing in this package talks to the network.
"""

from roadbed.asset.files import AssetTree, AssetValidationError, load, read_doc, schema_for, validate, validate_file
from roadbed.asset.hashing import jcs, jcs_sha256, merkle_root, object_key, row_hash, sha256_bytes, sha256_file
from roadbed.asset.lock import build_lock, check_lock, write_lock
from roadbed.asset.objects import ObjectError, ObjectStore
from roadbed.asset.overlap import Overlap, jaccard, minhash, shingles
from roadbed.asset.seal import SealError, seal_holdout
from roadbed.asset.signing import SignatureError, keyid, sign_file, verify_signature, write_signature
from roadbed.asset.verify import Check, VerifyFailed, VerifyResult, is_full, verify

__all__ = [
    "AssetTree", "AssetValidationError", "Check", "ObjectError", "ObjectStore", "Overlap", "SealError", "SignatureError",
    "VerifyFailed", "VerifyResult", "build_lock", "check_lock", "is_full", "jaccard", "jcs", "jcs_sha256", "keyid", "load",
    "merkle_root", "minhash", "object_key", "read_doc", "row_hash", "schema_for", "seal_holdout", "sha256_bytes",
    "sha256_file", "shingles", "sign_file", "validate", "validate_file", "verify", "verify_signature", "write_lock",
    "write_signature",
]
