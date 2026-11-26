"""Release gate: scores, contamination and license decision plus policy give pass or blocked, with reasons and findings."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from roadbed.contamination import problems as contamination_problems
from roadbed.contracts import (
    SPEC,
    Check,
    ContaminationReport,
    GateContaminationInput,
    GateFinding,
    GateLicenseInput,
    GatePolicy,
    GateResult,
    LicenseDecisionRecord,
    LicensePolicy,
    Scores,
)
from roadbed.hashing import sha256_canonical
from roadbed.stats import below_margin

Gate = GateResult
NO_TRAIN_MANIFEST = "train manifest absent: skipped"
LOCAL_REBASE = "not computed by a local rebase; roadbed verify checks disjointness"


def decide_license(license_id: str, policy: LicensePolicy, source: str = "policy/license.yaml") -> GateLicenseInput:
    lic = license_id.strip().lower()
    return GateLicenseInput(license=lic, allowed=lic in {a.lower() for a in policy.allow}, source=source)


def check_contamination(
    train_ids: Iterable[str] | None, holdout_ids: Iterable[str], train_manifest: str | None = None
) -> GateContaminationInput | None:
    """Shared ids between a train manifest and the holdout. None when there is no train manifest."""
    if train_ids is None:
        return None
    return GateContaminationInput(train_manifest=train_manifest, shared_ids=sorted(set(train_ids) & set(holdout_ids)))


def _fmt(x: float) -> str:
    return f"{x:+.1f}" if x else "0.0"


def _consistent(scores: Scores, policy: GatePolicy) -> None:
    """The gate's policy must be the one the scores were computed under; else policy_sha256 would mislead."""
    for name in ("seed", "n_boot", "ci"):
        if getattr(scores, name) != getattr(policy, name):
            raise ValueError(f"scores.{name} is {getattr(scores, name)} but the policy has {getattr(policy, name)}")
    for s in scores.slices:
        if s.low_n != (s.n < policy.min_n) or s.below_margin != below_margin(s.delta, s.n, policy):
            raise ValueError(f"slice {s.key}={s.value}: low_n/below_margin do not follow from the policy margin and min_n")


def _license_check(lic: GateLicenseInput | LicenseDecisionRecord, rebase: int, failed) -> Check:
    if isinstance(lic, LicenseDecisionRecord):
        if lic.rebase != rebase:
            raise ValueError(f"license.json is for rebase {lic.rebase}, scores are for rebase {rebase}")
        decision, source = lic.decision, lic.policy
    else:
        decision, source = ("allow" if lic.allowed else "deny"), lic.source
    if decision == "allow":
        return Check(name="license", status="pass", detail=f"{lic.license} allowed by {source}")
    if decision == "review":  # a human has not cleared it yet: fail closed
        return Check(name="license", status=failed("license_denied"), detail=f"{lic.license} needs review under {source}")
    return Check(name="license", status=failed("license_denied"), detail=f"{lic.license} denied by {source}")


def _contamination_check(cont: GateContaminationInput | ContaminationReport, rebase: int, failed) -> Check:
    if isinstance(cont, GateContaminationInput):  # Day 0 input: shared row ids only
        k = len(cont.shared_ids)
        return Check(name="contamination", status=failed("contamination") if k else "pass",
                     detail=f"{k} shared row ids between train manifest and holdout")
    if cont.rebase != rebase:
        raise ValueError(f"contamination.json is for rebase {cont.rebase}, scores are for rebase {rebase}")
    bad = contamination_problems(cont)
    if bad:
        return Check(name="contamination", status=failed("contamination"),
                     detail="contamination.json is inconsistent: " + "; ".join(bad))
    detail = f"{cont.exact.shared} shared row ids between train manifest and holdout"
    if cont.near.pairs:
        detail += (f"; {len(cont.near.pairs)} near-duplicate pairs ({cont.near.method} Jaccard >= {cont.near.threshold}, "
                   f"max {cont.near.max_jaccard})")
    if not cont.seal_order.ok:
        legs = [leg for leg in ("audit_seq", "timestamp", "git_ancestor") if not getattr(cont.seal_order, leg)]
        detail += "; seal does not precede training (" + ", ".join(legs) + ")"
    # SPEC.md: the gate's contamination check has the same status as contamination.json.
    return Check(name="contamination", status="pass" if cont.status == "pass" else failed("contamination"), detail=detail)


def evaluate(
    scores: Scores,
    contamination: ContaminationReport | GateContaminationInput | None,
    license: GateLicenseInput | LicenseDecisionRecord | None,
    policy: GatePolicy,
    *,
    finding_offset: int = 0,
    contamination_skip: str = NO_TRAIN_MANIFEST,
) -> GateResult:
    """Apply the gate policy to scores, the contamination report and the license decision (spec/METHOD.md).

    Checks, in order: slice_margin, license, contamination, then one skipped check per stub stage.
    A check that fails gets status "fail" when its reason is in policy.block_on, else "warn".
    Status is "blocked" when any check has status "fail". A missing input gives a skipped check.
    Findings are the below-margin slices in scores.slices order, with ids F-{finding_offset + 1:04d},
    F-{finding_offset + 2:04d}, ... Pass the number of findings already logged in the asset repo as finding_offset.
    Raises ValueError when the inputs disagree: scores computed under another seed, n_boot, ci, margin
    or min_n, or a contamination or license record for another rebase.
    contamination_skip is the detail of the skipped contamination check when contamination is None.
    """
    _consistent(scores, policy)

    def failed(reason: str) -> Literal["fail", "warn"]:
        return "fail" if reason in policy.block_on else "warn"

    checks: list[Check] = []

    below = [s for s in scores.slices if s.below_margin]
    low_n = sum(1 for s in scores.slices if s.low_n)
    detail = f"{len(below)} of {len(scores.slices)} slices below {policy.margin:.1f} margin"
    if below:
        detail += " (" + ", ".join(f"{s.key}={s.value} {_fmt(s.delta)}" for s in below) + ")"
    detail += f"; {low_n} flagged low-n"
    checks.append(Check(name="slice_margin", status=failed("slice_below_margin") if below else "pass", detail=detail))

    if license is None:
        checks.append(Check(name="license", status="skipped", detail="no license decision supplied"))
    else:
        checks.append(_license_check(license, scores.rebase, failed))

    if contamination is None:
        checks.append(Check(name="contamination", status="skipped", detail=contamination_skip))
    else:
        checks.append(_contamination_check(contamination, scores.rebase, failed))

    for name, stage in policy.stages.model_dump().items():
        if stage["stub"]:
            checks.append(Check(name=f"stage:{name}", status="skipped", detail="stub: not run"))

    findings = [
        GateFinding(
            id=f"F-{finding_offset + i:04d}",
            rebase=scores.rebase,
            slice=f"{s.key}={s.value}",
            n=s.n,
            delta=s.delta,
            ci_low=s.ci_low,
            ci_high=s.ci_high,
            margin=policy.margin,
        )
        for i, s in enumerate(below, 1)
    ]

    return GateResult(
        spec=SPEC,
        synthetic=scores.synthetic,
        rebase=scores.rebase,
        status="blocked" if any(c.status == "fail" for c in checks) else "pass",
        policy_sha256=sha256_canonical(policy.model_dump(mode="json", exclude_none=True)),
        checks=checks,
        findings=findings,
    )
