"""Hashing: golden vectors from spec/conformance and SPEC.md, Merkle trees of 1, 2, odd and 1000 leaves."""

from __future__ import annotations

import hashlib
import json
import random

import pytest

from roadbed.asset import jcs, jcs_sha256, merkle_root, object_key, read_doc, row_hash
from test_asset_support import CONFORMANCE, EXAMPLES

VECTORS = json.loads((CONFORMANCE / "vectors/hashing.json").read_text())


def _leaf(h: str) -> bytes:
    return hashlib.sha256(b"\x00" + bytes.fromhex(h)).digest()


def _node(a: bytes, b: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + a + b).digest()


def _reference(hashes: list[str]) -> str:
    """RFC 6962 MTH written out independently of the library: recursive, split at the largest power of two < n."""
    d = sorted(hashes)

    def mth(xs: list[str]) -> bytes:
        if not xs:
            return hashlib.sha256(b"").digest()
        if len(xs) == 1:
            return _leaf(xs[0])
        k = 1 << ((len(xs) - 1).bit_length() - 1)
        return _node(mth(xs[:k]), mth(xs[k:]))

    return mth(d).hex()


def _h(i: int) -> str:
    return hashlib.sha256(f"row-{i}".encode()).hexdigest()


@pytest.mark.parametrize("case", VECTORS["jcs"], ids=lambda c: c["sha256"][:8])
def test_jcs_vectors(case):
    assert jcs(case["object"]).decode() == case["jcs"]
    assert jcs_sha256(case["object"]) == case["sha256"]


@pytest.mark.parametrize("case", VECTORS["row_hash"], ids=lambda c: c["sha256"][:8])
def test_row_hash_vectors(case):
    assert row_hash(case["row"]) == case["sha256"]
    # extra keys (slices, row_hash) never enter the row hash
    assert row_hash({**case["row"], "slices": {"line": "x"}, "row_hash": "0" * 64}) == case["sha256"]


def test_row_hash_missing_key_is_null():
    row = dict(VECTORS["row_hash"][0]["row"])
    full = {**row, "outcome": None}
    row.pop("outcome")
    assert row_hash(row) == row_hash(full)


@pytest.mark.parametrize("case", VECTORS["merkle"], ids=lambda c: f"{len(c['leaves'])}-leaves")
def test_merkle_vectors(case):
    assert merkle_root(case["leaves"]) == case["root"]


def test_merkle_empty_is_sha256_of_nothing():
    assert merkle_root([]) == hashlib.sha256(b"").hexdigest() == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_merkle_one_leaf():
    assert merkle_root([_h(0)]) == _leaf(_h(0)).hex()


def test_merkle_two_leaves_sorted():
    a, b = sorted([_h(0), _h(1)])
    want = _node(_leaf(a), _leaf(b)).hex()
    assert merkle_root([b, a]) == merkle_root([a, b]) == want


def test_merkle_three_leaves_by_hand():
    a, b, c = sorted(_h(i) for i in range(3))
    # n=3 splits at k=2: MTH = node(node(leaf a, leaf b), leaf c)
    assert merkle_root([c, a, b]) == _node(_node(_leaf(a), _leaf(b)), _leaf(c)).hex()


@pytest.mark.parametrize("n", [3, 5, 7, 9, 31, 33, 999])
def test_merkle_odd_matches_reference(n):
    hs = [_h(i) for i in range(n)]
    assert merkle_root(hs) == _reference(hs)


def test_merkle_1000_leaves():
    hs = [_h(i) for i in range(1000)]
    root = merkle_root(hs)
    assert root == _reference(hs)
    shuffled = hs[:]
    random.Random(7).shuffle(shuffled)
    assert merkle_root(shuffled) == root  # sorted first: order of the input does not matter
    assert merkle_root(hs + [hs[0]]) != root  # duplicates are kept
    assert merkle_root(hs[:-1]) != root


def test_merkle_rejects_non_digest():
    with pytest.raises(ValueError):
        merkle_root(["ABC"])
    with pytest.raises(ValueError):
        merkle_root([_h(0).upper()])


@pytest.mark.parametrize("asset", ["pass", "blocked"])
def test_fixture_dataset_roots(asset):
    """Known answers: the stored row hashes and Merkle roots of the fixtures recompute from the rows."""
    root = EXAMPLES / asset
    man = read_doc(root / "data/snapshots/2026-11-01/MANIFEST.json")
    rows = read_doc(root / man["parts"][0]["path"])
    assert all(row_hash(r) == r["row_hash"] for r in rows)
    assert merkle_root(r["row_hash"] for r in rows) == man["merkle_root"]
    seal = read_doc(root / "data/holdout/H-2026-11-01/SEAL.json")
    hold = read_doc(root / "data/holdout/H-2026-11-01/holdout.jsonl")
    assert merkle_root(r["row_hash"] for r in hold) == seal["merkle_root"]


def test_receipt_chain_vector():
    """SPEC.md, Chain: prev_receipt is sha256(JCS(previous receipt.json)) of the stored JSON as parsed."""
    v12 = json.loads((EXAMPLES / "pass/versions/v12/receipt.json").read_text())
    r13 = json.loads((EXAMPLES / "pass/rebases/13/receipt.json").read_text())
    assert r13["predicate"]["prev_receipt"] == jcs_sha256(v12)


def test_object_key():
    oid = _h(1)
    assert object_key(oid) == f"objects/sha256/{oid[:2]}/{oid[2:4]}/{oid}"
    with pytest.raises(ValueError):
        object_key("../../etc/passwd")
