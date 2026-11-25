"""Eval synthesis: resolved rows -> frozen snapshot, holdout candidate, golden set, correction tests.

    from roadbed.evals import build
    out = build(resolved, slices, EvalPolicy(), seed=7, as_of="2026-11-01T09:00:00Z", output_schema=schema)

The library proposes; the validator seals. `holdout_candidate` is a proposal held in memory: `EvalBuild.write`
never puts it in the asset tree, and only `seal_candidate` (the validator's step, through `seal_holdout`)
turns it into `data/holdout/<id>/`. The control plane enforces who may call that (SoD-05).

Leakage rules, each checked at the end and proven in `proof` (a failed check raises LeakageError, nothing is
returned):

1. Holdout, golden and training rows are drawn from the labelled rows (approve and fix). Reject and escalate rows
   (`eval_only`) never enter any of them.
2. A row is a holdout or golden candidate only if no other labelled row shares its input_ref. Rows that share an
   input go to training together.
3. Train and holdout share no interaction id, no row hash, no input_ref, and no pair of inputs with word-shingle
   Jaccard >= `purge_threshold` (exact Jaccard, every pair that shares a shingle). The same holds for train and
   golden, golden and holdout, and the eval-only rows and holdout. Rows that break it are purged from the
   developer-visible side (training, golden, eval-only), never moved into the holdout.
4. Rows of an already sealed holdout (`sealed_holdout`) are left out of everything, and every developer-visible
   row is checked against them the same way.
5. Correction tests come from fixes in training and golden only; a fix whose row is in the holdout or was purged
   gets no test (counted in `report["edits"]`).
6. Rows of earlier snapshots (`trained_on`) and their near copies are never holdout or golden candidates: an
   incumbent model may have trained on them, and scoring it on them would flatter it.
7. Rows that were ever in a golden set or a correction test (`seen`) and their near copies are never holdout
   candidates: they are developer-visible, committed with their labels. `evals/golden.jsonl` and
   `evals/correction_tests.jsonl` are replaced on each build, so `evals/seen.jsonl` keeps the cumulative record:
   `read_seen` reads it (plus the two current files) and `EvalBuild.write` appends to it, never removes. A seen row
   may be golden again (no new exposure), and `write` refuses a candidate that holds a seen row.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from roadbed.asset import AssetValidationError, ObjectStore, merkle_root, seal_holdout, shingles, validate
from roadbed.asset.hashing import jcs_sha256, row_hash, sha256_bytes
from roadbed.contracts import GatePolicy, Slices
from roadbed.evals.split import draw, max_share_gap
from roadbed.labels.rules import LABEL_RULES_V1, LabelRules, check_pinned

SNAPSHOT_FIELDS = ("interaction_id", "input_ref", "output", "verdict", "reason_code", "outcome", "ts", "slices", "row_hash")
UTC_TS = re.compile(r"^(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}:\d{2}Z$")
SUFFIX = re.compile(r"^[a-z0-9]+$")
UNATTRIBUTED = "unattributed"
SYNTHETIC_TEXT = "# SYNTHETIC\n\nBuilt by roadbed evals build from a synthetic tenant. Illustrative data, not from any real tenant.\n"
SEEN = "evals/seen.jsonl"  # cumulative, append-only: every row that was ever in golden.jsonl or correction_tests.jsonl
SEEN_FIELDS = ("interaction_id", "input_ref", "row_hash", "first_seen")


class LeakageError(AssertionError):
    """A disjointness rule does not hold. Raised instead of returning a build."""


@dataclass(frozen=True)
class EvalPolicy:
    holdout_fraction: float = 0.15
    golden_fraction: float = 0.05
    near_threshold: float = 0.8  # policy/gate.yaml contamination.minhash_threshold: the gate's line
    purge_threshold: float = 0.6  # stricter line for the build, so a MinHash estimate near 0.8 cannot cross it
    shingle: int = 3
    num_perm: int = 128
    part_bytes: int = 1_000_000  # parts stay at or under 1 MB, so each fits in git (SPEC.md, Hashing rule 6)
    ct_start: int = 1

    def __post_init__(self) -> None:
        if not (0 < self.holdout_fraction < 1 and 0 <= self.golden_fraction < 1 and self.holdout_fraction + self.golden_fraction < 1):
            raise ValueError("holdout_fraction and golden_fraction must be in (0, 1) and sum below 1")
        if not 0 < self.purge_threshold <= self.near_threshold <= 1:
            raise ValueError("need 0 < purge_threshold <= near_threshold <= 1")

    @classmethod
    def from_gate(cls, gate: GatePolicy, **kw: Any) -> EvalPolicy:
        """Take threshold, shingle and num_perm from policy/gate.yaml, so the build and the gate agree."""
        c = gate.contamination
        if c is not None:
            kw.setdefault("near_threshold", c.minhash_threshold)
            kw.setdefault("shingle", c.shingle)
            kw.setdefault("num_perm", c.num_perm)
            kw.setdefault("purge_threshold", min(cls.purge_threshold, c.minhash_threshold))
        return cls(**kw)


@dataclass
class Part:
    name: str
    oid: str
    rows: int
    size: int
    path: str
    data: bytes = field(repr=False)


@dataclass
class Snapshot:
    manifest: dict[str, Any]
    parts: list[Part]
    rows: list[dict[str, Any]] = field(repr=False)

    @property
    def id(self) -> str:
        return str(self.manifest["id"])


@dataclass
class HoldoutCandidate:
    """A proposal for the validator. Rows and inputs are kept out of repr so they do not reach logs."""

    rows: list[dict[str, Any]] = field(repr=False)
    inputs: dict[str, str] = field(repr=False)
    stratification: dict[str, Any]
    seed: int
    snapshot: str
    merkle_root: str
    synthetic: bool

    def __len__(self) -> int:
        return len(self.rows)


@dataclass
class EvalBuild:
    snapshot: Snapshot
    holdout_candidate: HoldoutCandidate
    golden: list[dict[str, Any]]
    correction_tests: list[dict[str, Any]]
    eval_only: list[dict[str, Any]]
    proof: dict[str, Any]
    report: dict[str, Any]
    train_inputs: dict[str, str] = field(default_factory=dict, repr=False)  # input_ref -> raw text of each training row

    def write(self, root: str | Path, *, snapshots_dir: str = "data/snapshots", store: ObjectStore | None = None) -> list[str]:
        """Write the snapshot, golden set, correction tests and build report into the asset at root.

        Never writes the holdout candidate (see write_candidate). Refuses to overwrite a snapshot: it is frozen.
        Returns the paths written, from root.
        """
        return _write(self, Path(root), snapshots_dir, store)


def _date(as_of: str) -> str:
    m = UTC_TS.match(as_of)
    if not m:
        raise ValueError(f"as_of must be a UTC timestamp YYYY-MM-DDTHH:MM:SSZ, not {as_of!r}")
    return m.group(1)


def _keys(slices: Slices | Mapping[str, Any]) -> dict[str, list[str]]:
    s = slices if isinstance(slices, Slices) else Slices.model_validate(dict(slices))
    if not s.keys:
        raise ValueError("slices.yaml declares no slice keys")
    return {k: list(v) for k, v in s.keys.items()}


def _one(values: Iterable[Any], what: str) -> Any:
    vs = set(values)
    if len(vs) != 1:
        raise ValueError(f"a snapshot needs one {what}; the rows have {sorted(map(str, vs))}")
    return vs.pop()


def _proj(r: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    return {k: r[k] for k in fields}


def _validate_all(rows: Iterable[Mapping[str, Any]], schema: str) -> None:
    for i, r in enumerate(rows, 1):
        try:
            validate(dict(r), schema, strict=True)
        except AssetValidationError as e:
            raise ValueError(f"{schema} row {i}: {e}") from None


# ---------------------------------------------------------------- disjointness


class _Texts:
    """Word-shingle sets per interaction id, as small ints, with an inverted index per side."""

    def __init__(self, shingle: int) -> None:
        self.k = shingle
        self.vocab: dict[str, int] = {}
        self.sets: dict[str, frozenset[int]] = {}

    def add(self, iid: str, text: str) -> None:
        if iid not in self.sets:
            self.sets[iid] = frozenset(self.vocab.setdefault(s, len(self.vocab)) for s in shingles(text, self.k))

    def near(self, a: Iterable[str], b: Iterable[str], threshold: float) -> tuple[float, list[tuple[str, str, float]]]:
        """Exact Jaccard of every a x b pair that shares a shingle: (max Jaccard, pairs at or above threshold).
        A repeated id in b is indexed once, so its shingles are not counted twice."""
        index: dict[int, list[str]] = defaultdict(list)
        for bid in dict.fromkeys(b):
            for x in self.sets[bid]:
                index[x].append(bid)
        best, pairs = 0.0, []
        for aid in a:
            sa = self.sets[aid]
            shared: Counter[str] = Counter()
            for x in sa:
                shared.update(index.get(x, ()))
            for bid, inter in shared.items():
                sb = self.sets[bid]
                j = inter / (len(sa) + len(sb) - inter)
                best = max(best, j)
                if j >= threshold:
                    pairs.append((aid, bid, round(j, 6)))
        return round(best, 6), sorted(pairs)


def _pair_proof(a: Sequence[Mapping[str, Any]], b: Sequence[Mapping[str, Any]], texts: _Texts, threshold: float) -> dict[str, Any]:
    ids = {r["interaction_id"] for r in a} & {r["interaction_id"] for r in b}
    hashes = {r["row_hash"] for r in a} & {r["row_hash"] for r in b}
    refs = {r["input_ref"] for r in a} & {r["input_ref"] for r in b}
    with_text = [r["interaction_id"] for r in b if r["interaction_id"] in texts.sets]
    best, pairs = texts.near([r["interaction_id"] for r in a], with_text, threshold)
    return {
        "rows": [len(a), len(b)], "shared_interactions": len(ids), "shared_row_hashes": len(hashes), "shared_inputs": len(refs),
        "max_jaccard": best, "near_pairs": len(pairs), "without_text": len(b) - len(with_text),
    }


def assert_disjoint(
    train: Sequence[Mapping[str, Any]], holdout: Sequence[Mapping[str, Any]], texts: Mapping[str, str], *,
    threshold: float = 0.8, shingle: int = 3,
) -> dict[str, Any]:
    """Prove train and holdout disjoint: no shared interaction id, row hash or input_ref, and no input pair with
    word-shingle Jaccard >= threshold. `texts` maps input_ref to the input text. Raises LeakageError."""
    t = _Texts(shingle)
    a = [{**r, "interaction_id": r.get("interaction_id") or r["id"]} for r in train]
    b = [{**r, "interaction_id": r.get("interaction_id") or r["id"]} for r in holdout]
    for r in a + b:
        if r["input_ref"] in texts:
            t.add(r["interaction_id"], texts[r["input_ref"]])
    missing = [r["interaction_id"] for r in a if r["interaction_id"] not in t.sets]
    if missing:
        raise LeakageError(f"no input text for {len(missing)} training rows; near overlap cannot be measured")
    p = _pair_proof(a, b, t, threshold)
    _require_clean("train-holdout", p, threshold)
    return p


def _require_clean(name: str, p: Mapping[str, Any], threshold: float) -> None:
    bad = {k: p[k] for k in ("shared_interactions", "shared_row_hashes", "shared_inputs", "near_pairs", "without_text") if p[k]}
    if bad:
        raise LeakageError(f"{name} not disjoint (threshold {threshold}): {bad}")


# ---------------------------------------------------------------- build


def build(
    resolved: Sequence[Mapping[str, Any]],
    slices: Slices | Mapping[str, Any],
    policy: EvalPolicy | None = None,
    seed: int = 0,
    *,
    as_of: str,
    output_schema: Mapping[str, Any],
    created_at: str | None = None,
    rules: LabelRules = LABEL_RULES_V1,
    sealed_holdout: Sequence[Mapping[str, Any]] = (),
    sealed_synthetic: bool | None = None,
    trained_on: Sequence[Mapping[str, Any]] = (),
    seen: Sequence[Mapping[str, Any]] = (),
    suffix: str | None = None,
    parent: str | None = None,
) -> EvalBuild:
    """Build the frozen snapshot, holdout candidate, golden set and correction tests. See the module docstring.

    resolved: rows from roadbed.labels.resolve. slices: evals/slices.yaml. seed: sampling seed (recorded).
    as_of: the resolver's cutoff; the snapshot id is S-<date of as_of>[-suffix]. created_at defaults to as_of.
    sealed_holdout: rows of a holdout that is already sealed (holdout-row shape); left out of everything.
    sealed_synthetic: the synthetic flag of the seals (SEAL.json). Rows with the other flag are refused.
    trained_on: rows of earlier snapshots (snapshot-row shape). A model may already have trained on them, so they and
        their near copies are never holdout or golden candidates. They may enter training again. A row that carries
        a row_hash must hash to it.
    seen: rows that were ever in a golden set or a correction test (`read_seen`): never holdout candidates.
    """
    policy = policy or EvalPolicy()
    check_pinned(rules)
    date = _date(as_of)
    created_at = created_at or as_of
    _date(created_at)
    if suffix is not None and not SUFFIX.match(suffix):
        raise ValueError("suffix must be lowercase letters and digits")
    snap_id = f"S-{date}" + (f"-{suffix}" if suffix else "")
    declared = _keys(slices)
    keys = list(declared)

    live = [dict(r) for r in resolved if r["use"] != "quarantine"]
    quarantined = sorted(r["interaction_id"] for r in resolved if r["use"] == "quarantine")
    if len({r["interaction_id"] for r in live}) != len(live):
        raise ValueError("resolved rows repeat an interaction_id")
    for r in live:
        extra = set(r["slices"]) - set(declared)
        if extra:  # an undeclared key could carry anything (an email, a name) into every output file
            raise ValueError(f"{r['interaction_id']}: {len(extra)} slice key(s) not declared in slices.yaml")
        for k, vals in declared.items():
            if r["slices"].get(k) not in vals:
                raise ValueError(f"{r['interaction_id']}: slice {k} has a value not declared in slices.yaml")
    if not live:
        raise ValueError("no resolved rows to build from")
    synthetic = bool(_one((r["synthetic"] for r in live), "synthetic flag (do not mix synthetic and real rows)"))
    if not synthetic:  # a stream that lost its flag must not be labelled real
        syn = [r["interaction_id"] for r in live
               if any(str(r.get(k) or "").startswith("syn-") for k in ("interaction_id", "verdict_id", "outcome_id"))]
        if syn:
            raise ValueError(f"{len(syn)} rows have syn- ids but synthetic is not true (first {syn[0]}): refusing to label them real")
    if sealed_synthetic is not None and sealed_synthetic != synthetic:
        raise ValueError(f"the sealed holdout is {'synthetic' if sealed_synthetic else 'real'} and these rows are "
                         f"{'synthetic' if synthetic else 'real'}: they cannot share an asset (one asset, one synthetic flag)")
    profile = _one(((r["profile_id"], r["hmac_key_id"]) for r in live), "pseudonymisation profile and HMAC key")

    texts = _Texts(policy.shingle)
    for r in live:
        if "sha256:" + sha256_bytes(r["input_text"].encode("utf-8")) != r["input_ref"]:
            raise ValueError(f"{r['interaction_id']}: input_text does not hash to input_ref")
        texts.add(r["interaction_id"], r["input_text"])

    # 4. an already sealed holdout: out of everything
    sealed = [dict(r) for r in sealed_holdout]
    s_ids = {r["id"] for r in sealed}
    s_refs = {r["input_ref"] for r in sealed}
    s_hashes = {r["row_hash"] for r in sealed if r.get("row_hash")}
    by_iid = {r["interaction_id"]: r for r in live}
    # two seals can share rows: each sealed row counts once
    sealed_side = [dict(t) for t in dict.fromkeys(
        (("interaction_id", r["id"]), ("input_ref", r["input_ref"]), ("row_hash", r.get("row_hash") or "")) for r in sealed)]
    ref_text = {r["input_ref"]: r["input_text"] for r in live}
    for r in sealed_side:
        # The sealed row's text is the text of any live row with its input_ref (same id or not). A live row with the
        # sealed id but another input does not give the sealed row its text.
        src = by_iid.get(r["interaction_id"])
        if src is not None and src["input_ref"] != r["input_ref"]:
            texts.sets.pop(r["interaction_id"], None)
        if r["input_ref"] in ref_text:
            texts.add(r["interaction_id"], ref_text[r["input_ref"]])
    out_by_seal = {r["interaction_id"] for r in live
                   if r["interaction_id"] in s_ids or r["input_ref"] in s_refs or r["row_hash"] in s_hashes}
    live = [r for r in live if r["interaction_id"] not in out_by_seal]

    # 1-2. labelled pool; holdout and golden only from rows with a unique input
    pool = [r for r in live if r["use"] == "train"]
    eval_only = [r for r in live if r["use"] == "eval"]
    ref_n = Counter(r["input_ref"] for r in pool)
    candidates = [r for r in pool if ref_n[r["input_ref"]] == 1]
    by_ref = {r["input_ref"]: r["interaction_id"] for r in live}

    # 6-7. rows an incumbent may have trained on, and rows that were ever in an eval set, with their near copies
    def pinned(rows: Sequence[Mapping[str, Any]]) -> set[str]:
        ids, refs, hashes = ({r[k] for r in rows} for k in ("interaction_id", "input_ref", "row_hash"))
        with_text = sorted({i for i in ids if i in texts.sets} | {by_ref[f] for f in refs if f in by_ref})
        _, pairs = texts.near([r["interaction_id"] for r in candidates], with_text, policy.purge_threshold)
        return {a for a, _, _ in pairs} | {
            r["interaction_id"] for r in candidates if r["interaction_id"] in ids or r["input_ref"] in refs or r["row_hash"] in hashes}

    prior = _pinned_rows(trained_on, "trained_on", check_hash=True)
    seen_rows = _pinned_rows(seen, "seen", check_hash=False)  # the record keeps no fields to re-derive the hash from
    trained_before = pinned(prior)
    seen_before = pinned(seen_rows)
    eligible = sorted((r for r in candidates if r["interaction_id"] not in trained_before), key=lambda r: r["interaction_id"])
    hold_pool = [r for r in eligible if r["interaction_id"] not in seen_before]
    if not hold_pool:
        raise ValueError("no rows are eligible for the holdout (every labelled row shares an input, was trained on before "
                         "or was in a golden set or correction test before)")
    hold_ids = draw(hold_pool, keys, policy.holdout_fraction, seed, "holdout")
    rest = [r for r in eligible if r["interaction_id"] not in hold_ids]
    g_frac = policy.golden_fraction / (1 - policy.holdout_fraction)
    gold_ids = draw(rest, keys, g_frac, seed, "golden") if policy.golden_fraction > 0 else set()
    holdout = [r for r in hold_pool if r["interaction_id"] in hold_ids]
    golden = [r for r in rest if r["interaction_id"] in gold_ids]
    train = sorted((r for r in pool if r["interaction_id"] not in hold_ids | gold_ids), key=lambda r: r["interaction_id"])

    # 3-4. purge developer-visible rows near the holdout (and the sealed holdout), then training near golden
    th = policy.purge_threshold
    secret = [r["interaction_id"] for r in holdout] + [r["interaction_id"] for r in sealed_side if r["interaction_id"] in texts.sets]
    secret_refs = {r["input_ref"] for r in holdout} | s_refs

    def near_secret(rows: Sequence[Mapping[str, Any]]) -> set[str]:
        _, pairs = texts.near([r["interaction_id"] for r in rows], secret, th)
        return {a for a, _, _ in pairs} | {r["interaction_id"] for r in rows if r["input_ref"] in secret_refs}

    purged_golden = near_secret(golden)
    golden = [r for r in golden if r["interaction_id"] not in purged_golden]
    purged_eval = near_secret(eval_only)
    eval_only = [r for r in eval_only if r["interaction_id"] not in purged_eval]
    _, gpairs = texts.near([r["interaction_id"] for r in train], [r["interaction_id"] for r in golden], th)
    purged_train = near_secret(train) | {a for a, _, _ in gpairs} | {r["interaction_id"] for r in train if r["input_ref"] in {g["input_ref"] for g in golden}}
    train = [r for r in train if r["interaction_id"] not in purged_train]
    if not train:
        raise ValueError("no training rows left after the split")

    # proof, at the purge threshold (the stricter line); the gate threshold is reported next to it
    proof: dict[str, Any] = {"method": "jaccard", "shingle": policy.shingle, "purge_threshold": th, "gate_threshold": policy.near_threshold, "pairs": {}}
    for name, a, b in (("train-holdout", train, holdout), ("train-golden", train, golden), ("golden-holdout", golden, holdout),
                       ("eval_only-holdout", eval_only, holdout), ("train-sealed", train, sealed_side),
                       ("golden-sealed", golden, sealed_side), ("holdout-trained_on", holdout, prior),
                       ("golden-trained_on", golden, prior), ("holdout-seen", holdout, seen_rows)):
        p = _pair_proof(a, b, texts, th)
        if name.endswith(("sealed", "trained_on", "seen")):
            p["without_text"] = 0  # rows not in this event stream: exact checks only, counted here
            p["exact_only"] = len(b) - sum(1 for r in b if r["interaction_id"] in texts.sets)
        _require_clean(name, p, th)
        proof["pairs"][name] = p

    # snapshot
    snap_rows = sorted((_proj(r, SNAPSHOT_FIELDS) for r in train), key=lambda r: (r["row_hash"], r["interaction_id"]))
    _validate_all(snap_rows, "snapshot-row")
    parts = _parts(snap_rows, policy.part_bytes, snap_id)
    manifest: dict[str, Any] = {
        "spec": "roadbed/v0.1", "id": snap_id, "created_at": created_at, "rows": len(snap_rows),
        "schema_sha256": jcs_sha256(dict(output_schema)),
        "merkle_root": merkle_root(r["row_hash"] for r in snap_rows),
        "parts": [{"oid": p.oid, "rows": p.rows, "size": p.size, "path": p.path} for p in parts],
        "label_rules": rules.version,
        "pseudonymisation": {"profile": profile[0], "hmac_key_id": profile[1]},
        **({"parent": parent} if parent else {}),
        "synthetic": synthetic,
    }
    _validate_all([manifest], "snapshot-manifest")
    snapshot = Snapshot(manifest=manifest, parts=parts, rows=snap_rows)

    # holdout candidate and golden
    def eval_row(r: Mapping[str, Any], rid: str) -> dict[str, Any]:
        return {"id": rid, "slices": r["slices"], "input_ref": r["input_ref"], "expected": r["verdict"], "row_hash": r["row_hash"]}

    hrows = [eval_row(r, r["interaction_id"]) for r in holdout]
    _validate_all(hrows, "holdout-row")
    candidate = HoldoutCandidate(
        rows=hrows, inputs={r["input_ref"]: r["input_text"] for r in holdout},
        stratification={"keys": keys, "method": "proportional", "fraction": policy.holdout_fraction},
        seed=seed, snapshot=snap_id, merkle_root=merkle_root(r["row_hash"] for r in holdout), synthetic=synthetic,
    )
    grows = [eval_row(r, r["interaction_id"]) for r in golden]
    _validate_all(grows, "golden-row")

    # 5. one correction test per edit outside the holdout
    visible = sorted((r for r in train + golden if r["action"] == "fix"), key=lambda r: (r["ts"], r["interaction_id"]))
    cts = [
        {
            "id": f"CT-{n:04d}", "interaction_id": r["interaction_id"], "input_ref": r["input_ref"], "slices": r["slices"],
            "model_version": r["model_version"], "model_output": r["output"], "expected": r["fix"], "reason_code": r["reason_code"],
            "created_by": r["actor"] or UNATTRIBUTED, "created_at": r["ts"], "row_hash": r["row_hash"],
        }
        for n, r in enumerate(visible, policy.ct_start)
    ]
    _validate_all(cts, "correction-test-row")
    ev = [{"interaction_id": r["interaction_id"], "input_ref": r["input_ref"], "slices": r["slices"], "action": r["action"],
           "reason_code": r["reason_code"], "row_hash": r["row_hash"]} for r in eval_only]

    fixes_all = [r for r in resolved if r["use"] == "train" and r["action"] == "fix"]
    report = {
        "snapshot": snap_id, "as_of": as_of, "seed": seed, "synthetic": synthetic,
        "label_rules": {"version": rules.version, "sha256": rules.sha256},
        "counts": {
            "resolved": len(resolved), "quarantined": len(quarantined), "excluded_by_seal": len(out_by_seal),
            "labelled": len(pool), "shared_input_rows": len(pool) - len(candidates), "trained_before": len(trained_before),
            "seen_before": len(seen_before), "eligible": len(hold_pool), "eval_only": len(ev),
            "holdout_candidate": len(hrows), "golden": len(grows), "train": len(snap_rows), "correction_tests": len(cts),
            "outcome_overrides": sum(1 for r in train if r["label_source"] == "outcome"),
        },
        "purged": {"train": len(purged_train), "golden": len(purged_golden), "eval_only": len(purged_eval)},
        "edits": {
            "total": len(fixes_all), "correction_tests": len(cts),
            "withheld_holdout": sum(1 for r in holdout if r["action"] == "fix"),
            "withheld_purged": sum(1 for r in fixes_all if r["interaction_id"] in purged_train | purged_golden),
            "withheld_sealed": sum(1 for r in fixes_all if r["interaction_id"] in out_by_seal),
        },
        "stratification": {
            "keys": keys, "method": "proportional",
            "holdout_max_share_gap": round(max_share_gap(holdout, hold_pool, keys), 6),
            "golden_max_share_gap": round(max_share_gap(golden, eligible, keys), 6),
        },
        "quarantined": quarantined,
        "merkle": {"train": manifest["merkle_root"], "holdout_candidate": candidate.merkle_root},
    }
    return EvalBuild(snapshot=snapshot, holdout_candidate=candidate, golden=grows, correction_tests=cts, eval_only=ev,
                     proof=proof, report=report, train_inputs={r["input_ref"]: r["input_text"] for r in train})


def _pinned_rows(rows: Iterable[Mapping[str, Any]], what: str, *, check_hash: bool) -> list[dict[str, Any]]:
    """Rows to pin out of the holdout, each (interaction_id, input_ref, row_hash) once: snapshots repeat rows, and
    the dedup must not key on the hash alone (an empty or forged hash would collapse them to one row). With
    check_hash, a row that carries a row_hash must hash to it."""
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rows:
        iid, h = r.get("interaction_id") or r["id"], r.get("row_hash") or ""
        if check_hash and h and row_hash(r) != h:
            raise ValueError(f"{what}: row {iid} does not match its row_hash")
        out.setdefault((iid, r["input_ref"], h), {**r, "interaction_id": iid, "row_hash": h})
    return list(out.values())


def _jsonl(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows).encode("utf-8")


def _read_jsonl(p: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _parts(rows: Sequence[Mapping[str, Any]], limit: int, snap_id: str) -> list[Part]:
    chunks: list[list[bytes]] = [[]]
    size = 0
    for r in rows:
        line = _jsonl([r])
        if chunks[-1] and size + len(line) > limit:
            chunks.append([])
            size = 0
        chunks[-1].append(line)
        size += len(line)
    out = []
    for i, c in enumerate(chunks):
        data = b"".join(c)
        name = f"part-{i:04d}.jsonl"
        out.append(Part(name=name, oid=sha256_bytes(data), rows=len(c), size=len(data),
                        path=f"data/snapshots/{snap_id[2:]}/{name}", data=data))
    return out


# ---------------------------------------------------------------- files


def _check_one_flag(root: Path, snapshots_dir: str, synthetic: bool) -> None:
    """One asset, one synthetic flag: refuse a real build into a synthetic asset and the other way round. The flag
    is read from every snapshot manifest, every holdout seal and evals/SYNTHETIC.md."""
    base = root / snapshots_dir
    docs = (sorted(base.glob("*/MANIFEST.json")) if base.is_dir() else []) + sorted((root / "data" / "holdout").glob("*/SEAL.json"))
    flags = {bool(json.loads(m.read_text(encoding="utf-8")).get("synthetic")) for m in docs}
    if (root / "evals" / "SYNTHETIC.md").exists():
        flags.add(True)
    if flags - {synthetic}:
        have = "synthetic" if True in flags else "real"
        raise ValueError(f"the asset already holds {have} snapshots, sealed holdouts or evals; a "
                         f"{'synthetic' if synthetic else 'real'} build cannot go into it (one asset, one synthetic flag)")


def _check_unseen(candidate: HoldoutCandidate, seen: Sequence[Mapping[str, Any]]) -> None:
    """Rule 7 at write time: the candidate holds no row the asset's eval sets ever showed (exact keys)."""
    for key, k in (("id", "interaction_id"), ("input_ref", "input_ref"), ("row_hash", "row_hash")):
        shared = {r[key] for r in candidate.rows} & {r[k] for r in seen if r[k]}
        if shared:
            raise LeakageError(f"the holdout candidate holds {len(shared)} rows seen in golden or correction tests before "
                               f"(shared {k}); pass seen=read_seen(root) to build()")


def _write(b: EvalBuild, root: Path, snapshots_dir: str, store: ObjectStore | None) -> list[str]:
    snap_rel = f"{snapshots_dir.strip('/')}/{b.snapshot.id[2:]}"
    d = root / snap_rel
    if d.exists():
        raise FileExistsError(f"{snap_rel} exists: a snapshot is frozen, build a new one (suffix) instead")
    _check_one_flag(root, snapshots_dir, bool(b.report["synthetic"]))
    seen = read_seen(root)  # before the eval files are replaced
    _check_unseen(b.holdout_candidate, seen)
    d.mkdir(parents=True)
    written = []
    manifest = dict(b.snapshot.manifest)
    manifest["parts"] = [dict(p) for p in manifest["parts"]]
    for p, mp in zip(b.snapshot.parts, manifest["parts"]):
        (d / p.name).write_bytes(p.data)
        mp["path"] = f"{snap_rel}/{p.name}"
        written.append(mp["path"])
        if store is not None:
            store.put(p.data)
    if store is not None:  # the raw training inputs, under their input_ref, so verify can run the near check
        for text in b.train_inputs.values():
            store.put(text.encode("utf-8"))
    (d / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (d / "build.json").write_text(json.dumps({"report": b.report, "proof": b.proof}, indent=2) + "\n", encoding="utf-8")
    written += [f"{snap_rel}/MANIFEST.json", f"{snap_rel}/build.json"]
    ev = root / "evals"
    ev.mkdir(exist_ok=True)
    (ev / "golden.jsonl").write_bytes(_jsonl(b.golden))
    (ev / "correction_tests.jsonl").write_bytes(_jsonl(b.correction_tests))
    # rule 7: append this build's eval rows to the record; rows already there keep their first_seen
    have = {(r["interaction_id"], r["input_ref"], r["row_hash"]) for r in seen}
    new = sorted({(r.get("interaction_id") or r["id"], r["input_ref"], r["row_hash"]) for r in [*b.golden, *b.correction_tests]} - have)
    record = [{**r, "first_seen": r["first_seen"] or b.snapshot.id} for r in seen]
    record += [dict(zip(SEEN_FIELDS, (*k, b.snapshot.id))) for k in new]
    (root / SEEN).write_bytes(_jsonl(record))
    written += ["evals/golden.jsonl", "evals/correction_tests.jsonl", SEEN]
    if b.report["synthetic"]:
        for x in (d, ev):
            if not (x / "SYNTHETIC.md").exists():
                (x / "SYNTHETIC.md").write_text(SYNTHETIC_TEXT, encoding="utf-8")
                written.append((x / "SYNTHETIC.md").relative_to(root).as_posix())
    return written


def read_seen(root: str | Path) -> list[dict[str, Any]]:
    """Every row that was ever in evals/golden.jsonl or evals/correction_tests.jsonl: the record evals/seen.jsonl plus
    the two current files (a build from before the record existed left its rows only there). Each row is
    (interaction_id, input_ref, row_hash, first_seen); first_seen is the id of the build that first recorded the row,
    None for a row the record does not hold yet. Pass the result to build(seen=...)."""
    root = Path(root)
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    p = root / SEEN
    if p.is_file():
        for n, r in enumerate(_read_jsonl(p), 1):
            if any(not isinstance(r.get(k), str) for k in SEEN_FIELDS[:3]) or not isinstance(r.get("first_seen"), str):
                raise ValueError(f"{SEEN} line {n}: expected {', '.join(SEEN_FIELDS)}")
            out.setdefault((r["interaction_id"], r["input_ref"], r["row_hash"]), {k: r[k] for k in SEEN_FIELDS})
    for name, idk in (("golden.jsonl", "id"), ("correction_tests.jsonl", "interaction_id")):
        f = root / "evals" / name
        for r in _read_jsonl(f) if f.is_file() else []:
            row = {"interaction_id": r[idk], "input_ref": r["input_ref"], "row_hash": r.get("row_hash") or "", "first_seen": None}
            out.setdefault((row["interaction_id"], row["input_ref"], row["row_hash"]), row)
    return list(out.values())


def read_snapshots(root: str | Path, snapshots_dir: str = "data/snapshots", store: ObjectStore | None = None) -> list[dict[str, Any]]:
    """Rows of every snapshot under root. Each part is re-hashed against its oid, each row against its row_hash, and
    the Merkle root and row count against the manifest, so an edited part cannot hide rows a model trained on.
    A part that is neither in git nor in the store raises ValueError: the build must know every row a model may have
    trained on."""
    rows: list[dict[str, Any]] = []
    base = Path(root) / snapshots_dir
    for m in sorted(base.glob("*/MANIFEST.json")) if base.is_dir() else []:
        man = json.loads(m.read_text(encoding="utf-8"))
        snap: list[dict[str, Any]] = []
        for part in man["parts"]:
            p = Path(root) / part["path"] if part.get("path") else None
            if p is not None and p.is_file():
                data = p.read_bytes()
            elif store is not None and store.has(part["oid"]):
                data = store.get(part["oid"])
            else:
                raise ValueError(f"{man['id']}: part {part['oid'][:12]} is neither in the asset nor in the bucket (--objects)")
            if sha256_bytes(data) != part["oid"]:
                raise ValueError(f"{man['id']}: part {part['oid'][:12]} does not match its oid")
            snap += [json.loads(x) for x in data.decode("utf-8").splitlines() if x.strip()]
        if any(row_hash(r) != r.get("row_hash") for r in snap):
            raise ValueError(f"{man['id']}: a row does not match its row_hash")
        if len(snap) != man.get("rows") or merkle_root(r["row_hash"] for r in snap) != man.get("merkle_root"):
            raise ValueError(f"{man['id']}: rows do not match the manifest Merkle root or row count")
        rows += snap
    return rows


CANDIDATE_ROWS, CANDIDATE_INPUTS, CANDIDATE_META = "candidate.jsonl", "inputs.jsonl", "candidate.json"


def check_candidate_dir(out_dir: str | Path, asset_root: str | Path | None) -> Path:
    """The resolved candidate directory. Raises ValueError when it is inside the asset tree."""
    out = Path(out_dir).resolve()
    if asset_root is not None:
        root = Path(asset_root).resolve()
        if out == root or root in out.parents:
            raise ValueError("the holdout candidate goes to the validator, never into the asset tree")
    return out


def write_candidate(c: HoldoutCandidate, out_dir: str | Path, *, asset_root: str | Path | None = None) -> list[Path]:
    """Hand the holdout candidate to the validator: write it to out_dir, files mode 0600.

    Refuses a directory inside the asset tree, where developers would read it.
    """
    out = check_candidate_dir(out_dir, asset_root)
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    meta = {"stratification": c.stratification, "seed": c.seed, "snapshot": c.snapshot, "merkle_root": c.merkle_root,
            "rows": len(c.rows), "synthetic": c.synthetic}
    files = {
        CANDIDATE_ROWS: _jsonl(c.rows),
        CANDIDATE_INPUTS: _jsonl({"input_ref": k, "text": v} for k, v in sorted(c.inputs.items())),
        CANDIDATE_META: (json.dumps(meta, indent=2) + "\n").encode(),
    }
    paths = []
    for name, data in files.items():
        p = out / name
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        paths.append(p)
    return paths


def read_candidate(out_dir: str | Path) -> HoldoutCandidate:
    d = Path(out_dir)
    meta = json.loads((d / CANDIDATE_META).read_text(encoding="utf-8"))
    rows = [json.loads(x) for x in (d / CANDIDATE_ROWS).read_text(encoding="utf-8").splitlines() if x.strip()]
    inputs = {o["input_ref"]: o["text"] for o in (json.loads(x) for x in (d / CANDIDATE_INPUTS).read_text(encoding="utf-8").splitlines() if x.strip())}
    c = HoldoutCandidate(rows=rows, inputs=inputs, stratification=meta["stratification"], seed=meta["seed"],
                         snapshot=meta["snapshot"], merkle_root=meta["merkle_root"], synthetic=meta["synthetic"])
    if merkle_root(r["row_hash"] for r in rows) != c.merkle_root:
        raise ValueError("candidate rows do not match the candidate Merkle root")
    return c


def seal_candidate(
    c: HoldoutCandidate, root: str | Path, holdout_id: str, *, sealed_by: str, sealed_at: str, audit_seq: int | None,
    store: ObjectStore, num_perm: int = 128, shingle: int = 3, provenance: Literal["control_plane", "local"] = "control_plane",
) -> dict[str, Any]:
    """The validator's step: seal the candidate through seal_holdout (full seal: stratification, snapshot,
    MinHash object). The caller must be the validator (SoD-05, enforced by the control plane). A local seal
    (`roadbed evals seal`) passes provenance="local" and audit_seq=None: it names a person, no role is enforced."""
    return seal_holdout(
        root, holdout_id, c.rows, sealed_by=sealed_by, sealed_at=sealed_at, audit_seq=audit_seq, seed=c.seed,
        synthetic=c.synthetic, stratification=c.stratification, snapshot=c.snapshot, inputs=c.inputs, store=store,
        num_perm=num_perm, shingle=shingle, provenance=provenance,
    )


