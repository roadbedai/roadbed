"""Release gate: turns scores plus policy into pass or blocked, with findings."""

from __future__ import annotations

from collections.abc import Iterable

from roadbed.contracts import (
    SPEC,
    Check,
    GateContaminationInput,
    GateFinding,
    GateLicenseInput,
    GatePolicy,
    GateResult,
    LicensePolicy,
    Scores,
)
from roadbed.hashing import sha256_canonical


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


def evaluate(
    scores: Scores,
    policy: GatePolicy,
    license_decision: GateLicenseInput | None = None,
    contamination: GateContaminationInput | None = None,
    *,
    finding_offset: int = 0,
) -> GateResult:
    """Apply the gate policy to scores.

    A check that fails gets status "fail" when its reason is in policy.block_on,
    else "warn". Status is "blocked" when any check has status "fail".
    Findings are the below-margin slices in scores.slices order, with ids
    F-{finding_offset + 1:04d}, F-{finding_offset + 2:04d}, ... Pass the number of
    findings already logged in the asset repo as finding_offset.
    """

    def failed(reason: str) -> str:
        return "fail" if reason in policy.block_on else "warn"

    checks: list[Check] = []

    below = [s for s in scores.slices if s.below_margin]
    low_n = sum(1 for s in scores.slices if s.low_n)
    detail = f"{len(below)} of {len(scores.slices)} slices below {policy.margin:.1f} margin"
    if below:
        detail += " (" + ", ".join(f"{s.key}={s.value} {_fmt(s.delta)}" for s in below) + ")"
    detail += f"; {low_n} flagged low-n"
    checks.append(Check(name="slice_margin", status=failed("slice_below_margin") if below else "pass", detail=detail))

    if license_decision is None:
        checks.append(Check(name="license", status="skipped", detail="no license decision supplied"))
    else:
        verb = "allowed" if license_decision.allowed else "denied"
        checks.append(
            Check(
                name="license",
                status="pass" if license_decision.allowed else failed("license_denied"),
                detail=f"{license_decision.license} {verb} by {license_decision.source}",
            )
        )

    if contamination is None:
        checks.append(Check(name="contamination", status="skipped", detail="train manifest absent: skipped"))
    else:
        k = len(contamination.shared_ids)
        checks.append(
            Check(
                name="contamination",
                status=failed("contamination") if k else "pass",
                detail=f"{k} shared row ids between train manifest and holdout",
            )
        )

    for name, stage in policy.stages.model_dump().items():
        if stage["stub"]:
            checks.append(Check(name=f"stage:{name}", status="skipped", detail="stub: not run in prototype"))

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
