"""`roadbed rebase`: score supplied predictions, apply the gate, write the rebase files."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from roadbed.cli.asset import (
    MANIFEST,
    NOTICE,
    PRED_FILES,
    PROPOSED,
    REBASE_META,
    Asset,
    AssetError,
    bound_files,
    load_rows,
    predictions_supplied,
    read_meta,
    receipt_executor,
    seal_file,
)
from roadbed.cli.output import MINUS, plain, signed
from roadbed.contracts import FileDigest, GateContaminationInput, GateResult, HoldoutDigest, PredictionDigests, Scores
from roadbed.gate import decide_license, evaluate
from roadbed.hashing import sha256_file
from roadbed.io import read_yaml, write_yaml
from roadbed.stats import compare

FINDING_RE = re.compile(r"^F-(\d{4})\.yaml$")


@dataclass
class BaseRef:
    repo: str
    revision: str = "unpinned"
    license: str | None = None


@dataclass
class RebaseResult:
    n: int
    base: BaseRef
    scores: Scores
    gate: GateResult
    final: str
    files: list[str] = field(default_factory=list)


def resolve_base(meta: dict[str, Any] | None, repo: str | None, revision: str | None, license_id: str | None,
                 cached: dict[str, Any] | None) -> BaseRef:
    """CLI options win, then rebases/<n>/rebase.yaml, then the last `roadbed watch` result."""
    src: dict[str, Any] = {}
    if meta and isinstance(meta.get("base"), dict):
        src = dict(meta["base"])
    elif cached:
        src = {"repo": cached.get("repo"), "revision": cached.get("sha"), "license": cached.get("license")}
    if repo:
        if repo != src.get("repo"):
            src = {}
        src["repo"] = repo
    if revision:
        src["revision"] = revision
    if license_id:
        src["license"] = license_id
    if not src.get("repo"):
        raise AssetError("no base model: pass --base, add rebases/<n>/rebase.yaml, or run `roadbed watch` first")
    rev = src.get("revision") or "unpinned"
    if not re.fullmatch(r"[0-9a-f]{40}|unpinned", str(rev)):
        raise AssetError(f"base revision must be 40 hex characters or 'unpinned' (got {rev})")
    lic = src.get("license")
    return BaseRef(repo=str(src["repo"]), revision=str(rev), license=str(lic).lower() if lic else None)


def _finding_offset(findings_dir: Path, n: int) -> int:
    """Highest finding number logged by other rebases. Stale findings of rebase n are removed."""
    top = 0
    if not findings_dir.is_dir():
        return 0
    for f in sorted(findings_dir.iterdir()):
        m = FINDING_RE.match(f.name)
        if not m:
            continue
        data = read_yaml(f) or {}
        if data.get("rebase") == n:
            f.unlink()
        else:
            top = max(top, int(m.group(1)))
    return top


def _iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def final_line(n: int, gate: GateResult, margin: float, *, local: bool = False) -> str:
    """local: the holdout has a local seal (no control plane, no sign-off); a pass says so."""
    if gate.status == "pass":
        if local:
            return f"rebase #{n}: gate pass, local, not attested; commit it, then roadbed verify rebases/{n}"
        return f"opened rebase #{n} → awaiting MRM sign-off"
    blocking = gate.findings
    if blocking and any(c.name == "slice_margin" and c.status == "fail" for c in gate.checks):
        parts = ", ".join(f"{f.slice} {signed(f.delta)}" for f in blocking)
        ids = ", ".join(f.id for f in blocking)
        return f"blocked: {parts} below {plain(margin)} margin · {ids} logged"
    failed = "; ".join(f"{c.name} {c.detail}" for c in gate.checks if c.status == "fail")
    return f"blocked: {failed}"


def summary_line(scores: Scores, margin: float) -> str:
    o = scores.overall
    below = sum(s.below_margin for s in scores.slices)
    low = sum(s.low_n for s in scores.slices)
    return (
        f"{signed(o.delta)} (CI {signed(o.ci_low)} to {signed(o.ci_high)}) · "
        f"{below} of {len(scores.slices)} slices below {plain(margin)} margin · {low} flagged low-n"
    )


def _num(x: float) -> str:
    return f"{x:.1f}".replace("-", MINUS)


def provenance_line(executor: str | None, synthetic: bool) -> str:
    """First line of a report that is not the synthetic Day 0 one: who produced the predictions, and the data kind."""
    if executor is None:
        who = "Executor: none, predictions supplied with `--from-predictions`, no training in this run."
    elif predictions_supplied(executor):
        who = f"Executor: {executor}, predictions supplied, no model was trained."
    else:
        who = f"Executor: {executor}, predictions from the {executor} eval job."
    data = "Data: SYNTHETIC, illustrative only." if synthetic else "Data: real."
    return f"> **{who} {data}** The scoring, statistics, gate and hashes are the real code paths."


def report_md(asset: Asset, n: int, base: BaseRef, scores: Scores, gate: GateResult, final: str,
              executor: str | None = None) -> str:
    """The PR flow uses this file verbatim as the PR body. A synthetic asset with supplied predictions keeps the Day 0 text;
    any other report names the executor and the data kind on its first line and carries no SYNTHETIC mark when the
    asset is real."""
    cfg = asset.cfg
    synthetic = bool(cfg["synthetic"])
    day0 = synthetic and predictions_supplied(executor)
    o = scores.overall
    below = [s for s in scores.slices if s.below_margin]
    low = [s for s in scores.slices if s.low_n]
    verdict = (
        "The gate passed. No slice is below the margin."
        if gate.status == "pass"
        else f"The gate blocked this rebase: {len(below)} slice(s) fell below the {plain(asset.policy.margin)} margin."
        + (" The overall delta alone would not have caught it." if o.delta >= 0 else "")
    )
    summary = (
        f"Rebase #{n} moves `{cfg['model']}` for {cfg['tenant']} from `{cfg['base']['repo']}` to "
        f"`{base.repo}` @ `{base.revision[:12]}`. Both models were scored on {o.n:,} sealed holdout cases "
        f"(holdout `{scores.holdout.id}`). The candidate scores {_num(o.candidate)} against the incumbent's "
        f"{_num(o.incumbent)}, a delta of {signed(o.delta)} ({int(scores.ci * 100)}% CI {signed(o.ci_low)} to "
        f"{signed(o.ci_high)}). {verdict} {len(low)} slice(s) have fewer than {asset.policy.min_n} rows and are "
        f"flagged low-n; they never block."
    )
    if day0:
        out = [
            f"# Rebase #{n}: {cfg['model']} → {base.repo} (SYNTHETIC)",
            "",
            summary,
            "",
            "> **Illustrative: predictions supplied, no training in this run.** "
            "The prediction files are synthetic fixtures passed with `--from-predictions`. "
            "The scoring, statistics, gate and hashes are the real code paths. All data in this repo is synthetic.",
            "",
        ]
    else:
        out = [f"# Rebase #{n}: {base.repo} for {cfg['model']}", "", provenance_line(executor, synthetic), "", summary, ""]
    out += [
        "## Results",
        "",
        "| Slice | n | Incumbent | Candidate | Delta | 95% CI | Flag |".replace("95%", f"{int(scores.ci * 100)}%"),
        "|---|---:|---:|---:|---:|---|---|",
        f"| **overall** | {o.n:,} | {_num(o.incumbent)} | {_num(o.candidate)} | {signed(o.delta)} | "
        f"{signed(o.ci_low)} to {signed(o.ci_high)} | |",
    ]
    for s in scores.slices:
        flag = "below margin" if s.below_margin else "low-n" if s.low_n else ""
        out.append(
            f"| {s.key}={s.value} | {s.n:,} | {_num(s.incumbent)} | {_num(s.candidate)} | {signed(s.delta)} | "
            f"{signed(s.ci_low)} to {signed(s.ci_high)} | {flag} |"
        )
    out += ["", "## Gate checks", "", "| Check | Status | Detail |", "|---|---|---|"]
    for c in gate.checks:
        out.append(f"| {c.name} | {c.status} | {c.detail} |")
    if gate.findings:
        out += ["", "## Findings", ""]
        for f in gate.findings:
            out.append(
                f"- {f.id}: {f.slice} {signed(f.delta)} (CI {signed(f.ci_low)} to {signed(f.ci_high)}, n={f.n}) "
                f"below {plain(f.margin)} margin"
            )
    out += [
        "",
        f"Policy sha256 `{gate.policy_sha256}` · seed {scores.seed} · {scores.n_boot:,} bootstrap resamples. "
        f"Run `roadbed verify rebases/{n}` to recompute every number.",
        "",
        final,
        "",
    ]
    return "\n".join(out)


def write_manifest(asset: Asset, d: Path) -> Path:
    """Hash every file of the rebase plus the holdout. evidence/ has its own signed MANIFEST (SPEC.md)."""
    files = sorted(
        p for p in d.rglob("*") if p.is_file() and p.name != MANIFEST and "evidence" not in p.relative_to(d).parts
    )
    files.append(asset.holdout_file)
    files.extend(bound_files(asset))
    lines = [f"{sha256_file(p)}  {asset.rel(p)}" for p in sorted(files, key=lambda p: asset.rel(p))]
    m = d / MANIFEST
    m.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return m


def run(asset: Asset, d: Path, n: int, base: BaseRef, emit, contamination: GateContaminationInput | None = None) -> RebaseResult:
    """contamination: overlap between the training snapshot and the holdout; None means no train manifest (skipped).

    A receipt in the rebase directory names the executor. Predictions are "supplied" unless a training executor
    (runner, hf-jobs, together) produced them.
    """
    cfg = asset.cfg
    synthetic = bool(cfg["synthetic"])
    meta = read_meta(d)
    executor = receipt_executor(d)
    notice = NOTICE if predictions_supplied(executor) else f"predictions from the {executor} eval job"
    emit(f"new base: {base.repo}")
    emit(notice)

    hold, inc, cand = load_rows(asset, d)
    scores = compare(
        inc, cand, hold, asset.slices, asset.policy,
        scorers=asset.scorers, synthetic=synthetic, model=cfg["model"], rebase=n,
        holdout_digest=HoldoutDigest(id=cfg["data"]["holdout"]["id"], rows=len(hold), sha256=sha256_file(asset.holdout_file)),
        prediction_digests=PredictionDigests(**{
            k: FileDigest(path=asset.rel(d / PRED_FILES[k]), sha256=sha256_file(d / PRED_FILES[k]))
            for k in ("incumbent", "candidate")
        }),
        notice=notice,
    )
    emit(f"scored on {scores.overall.n:,} sealed holdout cases")

    lic = None
    if base.license and asset.license_policy is not None:
        lic = decide_license(base.license, asset.license_policy)
        emit(f"license {lic.license}: {'allowed' if lic.allowed else 'denied'} by policy")

    findings_dir = asset.root / "findings"
    gate = evaluate(scores, contamination, lic, asset.policy, finding_offset=_finding_offset(findings_dir, n))
    emit(summary_line(scores, asset.policy.margin))

    # rebase.yaml records which base was used and when the rebase was created.
    new_meta = {
        **(meta or {}),
        "synthetic": synthetic,
        "rebase": n,
        "model": cfg["model"],
        "base": {"repo": base.repo, "revision": base.revision, **({"license": base.license} if base.license else {})},
        "created_at": (meta or {}).get("created_at") or _iso_now(),
    }
    if meta != new_meta:
        if isinstance(new_meta["created_at"], datetime):
            new_meta["created_at"] = new_meta["created_at"].isoformat().replace("+00:00", "Z")
        write_yaml(d / REBASE_META, new_meta)

    (d / "scores.json").write_text(scores.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")
    (d / "gate.json").write_text(gate.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")
    sf = seal_file(asset)
    local = sf is not None and (read_yaml(sf) or {}).get("provenance") == "local"
    final = final_line(n, gate, asset.policy.margin, local=local)
    (d / "report.md").write_text(report_md(asset, n, base, scores, gate, final, executor), encoding="utf-8")

    written = [d / REBASE_META, d / "scores.json", d / "gate.json", d / "report.md"]
    for f in gate.findings:
        p = findings_dir / f"{f.id}.yaml"
        write_yaml(p, {**f.model_dump(mode="json"), "synthetic": synthetic, "gate": asset.rel(d / "gate.json")})
        written.append(p)

    proposed = d / PROPOSED
    if gate.status == "pass":
        new_cfg = copy.deepcopy(cfg)
        new_cfg["base"] = {"repo": base.repo, "revision": base.revision}
        write_yaml(proposed, new_cfg)
        proposed.write_text(
            f"# {'SYNTHETIC · ' if synthetic else ''}proposed by `roadbed rebase` #{n}: base bump, applied by the PR script "
            "after MRM sign-off\n"
            + proposed.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        written.append(proposed)
    elif proposed.exists():
        proposed.unlink()

    written.append(write_manifest(asset, d))
    return RebaseResult(n=n, base=base, scores=scores, gate=gate, final=final, files=[asset.rel(p) for p in written])
