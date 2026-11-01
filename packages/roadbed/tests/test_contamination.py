"""roadbed.contamination: exact row-hash overlap and MinHash near overlap, with hand-worked answers."""

from __future__ import annotations

import hashlib
import random

import jsonschema
import pytest

from conftest import make_policy
from roadbed.contamination import check, jaccard, problems, shingles, signature
from roadbed.contracts import ContaminationPolicy, DatasetRef, SealOrder, json_schema
from roadbed.gate import evaluate

POLICY = make_policy(contamination=ContaminationPolicy(minhash_threshold=0.8, num_perm=128, shingle=3))
SEAL_OK = SealOrder(seal_commit="1" * 40, train_commit="2" * 40, audit_seq=True, timestamp=True, git_ancestor=True, ok=True)
WORDS = [f"w{i}" for i in range(400)]


def _text(seed: str, n: int = 30) -> str:
    rng = random.Random(seed)
    return " ".join(rng.choice(WORDS) for _ in range(n))


def _ref(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _row(text: str, tag: str) -> dict:
    return {"id": tag, "input_ref": _ref(text), "row_hash": hashlib.sha256(tag.encode()).hexdigest()}


def _run(train_texts, hold_texts, *, method="minhash", leak=0, seal=SEAL_OK):
    hold = [_row(t, f"h{i}") for i, t in enumerate(hold_texts)]
    train = [_row(t, f"t{i}") for i, t in enumerate(train_texts)] + hold[:leak]  # leaked rows share the row hash
    texts = {r["input_ref"]: t for r, t in zip(hold, hold_texts)} | {r["input_ref"]: t for r, t in zip(train, train_texts)}
    rep = check(train, hold, POLICY, texts=texts, rebase=14, synthetic=True, method=method, seal_order=seal,
                holdout=DatasetRef(id="H-1", merkle_root="a" * 64, rows=len(hold)),
                train=DatasetRef(id="S-2026-11-01", merkle_root="b" * 64, rows=len(train)))
    jsonschema.Draft202012Validator(json_schema("contamination")).validate(rep.model_dump(mode="json", exclude_none=True))
    assert problems(rep) == []
    return hold, rep


def test_shingles_and_jaccard_by_hand():
    assert shingles("a b c d", 3) == {"a b c", "b c d"}
    assert shingles("a b", 3) == {"a b"}  # shorter than k: the whole text is one shingle
    assert shingles("   ", 3) == set()
    assert jaccard({"x", "y"}, {"y", "z"}) == pytest.approx(1 / 3)
    assert jaccard(set(), set()) == 0.0


def test_minhash_signature_format():
    sig = signature({"a b c"}, 4)
    assert sig == [hashlib.sha256(f"{j}|a b c".encode()).digest()[:8].hex() for j in range(4)]
    with pytest.raises(ValueError):
        signature(set(), 4)


def test_clean_sets_pass():
    _, rep = _run([_text(f"t{i}") for i in range(30)], [_text(f"h{i}") for i in range(20)])
    assert rep.status == "pass" and rep.exact.shared == 0 and rep.near.pairs == []
    assert 0 <= rep.near.max_jaccard < 0.8


def test_three_leaked_rows_fail_and_block():
    hold_texts = [_text(f"h{i}") for i in range(20)]
    hold, rep = _run([_text(f"t{i}") for i in range(30)], hold_texts, leak=3)
    assert rep.status == "fail" and rep.exact.shared == 3
    assert rep.exact.row_hashes == sorted(r["row_hash"] for r in hold[:3])
    # identical texts are also near pairs, Jaccard 1.0
    assert {p.holdout for p in rep.near.pairs} == {r["row_hash"] for r in hold[:3]}
    assert all(p.jaccard == 1.0 for p in rep.near.pairs) and rep.near.max_jaccard == 1.0


def test_near_duplicate_found_by_minhash_and_exact():
    """A holdout text with 1 of 40 words changed: 3-shingle Jaccard is 35/41 (by hand) and both methods flag it."""
    base = _text("dup", 40)
    words = base.split()
    edited = " ".join(words[:20] + ["CHANGED"] + words[21:])
    sa, sb = shingles(base, 3), shingles(edited, 3)
    assert (len(sa & sb), len(sa | sb)) == (35, 41)  # 38 shingles each; 3 contain word 20
    train = [_text(f"t{i}") for i in range(10)] + [edited]
    for method in ("jaccard", "minhash"):
        hold, rep = _run(train, [base] + [_text(f"h{i}") for i in range(5)], method=method)
        assert rep.status == "fail" and rep.exact.shared == 0
        [p] = rep.near.pairs
        assert p.holdout == hold[0]["row_hash"]
        if method == "jaccard":
            assert p.jaccard == round(35 / 41, 6)
        else:  # 128-slot estimate; standard error sqrt(J(1-J)/128) is about 0.03
            assert abs(p.jaccard - 35 / 41) < 0.12


def test_minhash_estimate_tracks_exact_jaccard():
    """Over pairs with known overlap, the MinHash estimate stays within 4 standard errors of the exact value."""
    for keep in (10, 20, 30, 38):
        a = _text("est", 40).split()
        b = a[:keep] + _text(f"tail{keep}", 40 - keep).split()
        sa, sb = shingles(" ".join(a), 3), shingles(" ".join(b), 3)
        exact = jaccard(sa, sb)
        sig_a, sig_b = signature(sa, 128), signature(sb, 128)
        est = sum(x == y for x, y in zip(sig_a, sig_b)) / 128
        se = max((exact * (1 - exact) / 128) ** 0.5, 0.01)
        assert abs(est - exact) <= 4 * se, (keep, exact, est)


def test_seal_order_failure_fails_the_report():
    bad = SealOrder(seal_commit="1" * 40, train_commit="2" * 40, audit_seq=True, timestamp=False, git_ancestor=True, ok=False)
    _, rep = _run([_text("t")], [_text("h")], seal=bad)
    assert rep.status == "fail" and rep.exact.shared == 0 and not rep.near.pairs


def test_input_errors():
    hold = [_row("x y z", "h0")]
    texts = {hold[0]["input_ref"]: "x y z"}
    kw = dict(rebase=1, synthetic=True, seal_order=SEAL_OK, holdout=DatasetRef(id="H-1", merkle_root="a" * 64, rows=1),
              train=DatasetRef(id="S-2026-11-01", merkle_root="b" * 64, rows=1))
    train = [_row("p q r", "t0")]
    with pytest.raises(ValueError, match="no text"):
        check(train, hold, POLICY, texts=texts, **kw)
    with pytest.raises(ValueError, match="does not hash"):
        check(train, hold, POLICY, texts={**texts, train[0]["input_ref"]: "tampered"}, **kw)
    with pytest.raises(ValueError, match="row_hash"):
        check([{**train[0], "row_hash": None}], hold, POLICY, texts=texts, **kw)
    with pytest.raises(ValueError, match="rows: 1 but 2"):
        check(train * 2, hold, POLICY, texts={**texts, train[0]["input_ref"]: "p q r"}, **kw)
    with pytest.raises(ValueError, match="no contamination settings"):
        check(train, hold, make_policy(), texts=texts, **kw)


def test_gate_uses_the_report_status():
    from test_gate import _scores

    _, clean = _run([_text(f"t{i}") for i in range(5)], [_text(f"h{i}") for i in range(5)])
    _, dirty = _run([_text(f"t{i}") for i in range(5)], [_text(f"h{i}") for i in range(5)], leak=3)
    s = _scores(0, rebase=14)
    assert evaluate(s, clean, None, make_policy()).status == "pass"
    g = evaluate(s, dirty, None, make_policy())
    assert g.status == "blocked"
    assert next(c for c in g.checks if c.name == "contamination").detail.startswith("3 shared row ids")
