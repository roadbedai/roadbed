"""Eval synthesis (W14): frozen snapshot, holdout candidate, golden set and correction tests from resolved rows.

    from roadbed.labels import resolve
    from roadbed.evals import build, EvalPolicy
    out = build(resolve(...), slices, EvalPolicy(), seed=7, as_of="2026-11-01T09:00:00Z", output_schema=schema)
    out.write(asset_root)                         # snapshot, golden, correction tests, build report
    write_candidate(out.holdout_candidate, dir)   # to the validator, never into the asset
"""

from roadbed.evals.build import (
    EvalBuild,
    EvalPolicy,
    HoldoutCandidate,
    LeakageError,
    Part,
    Snapshot,
    assert_disjoint,
    build,
    read_candidate,
    read_snapshots,
    seal_candidate,
    write_candidate,
)

__all__ = [
    "EvalBuild", "EvalPolicy", "HoldoutCandidate", "LeakageError", "Part", "Snapshot", "assert_disjoint", "build",
    "read_candidate", "read_snapshots", "seal_candidate", "write_candidate",
]
