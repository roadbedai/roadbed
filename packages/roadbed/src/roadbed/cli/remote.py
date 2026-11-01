"""Remote commands over the control plane: rebase (follow the timeline), status, seal, promote, rollback.

Every number printed here comes from the response body (scores.json, gate.json, receipt.json as the control plane
serves them). Mock, replay and synthetic output is labelled on screen and in --json.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from roadbed.cli.api import Api, ApiError, seg
from roadbed.cli.output import signed
from roadbed.cli.rebase import final_line
from roadbed.contracts import AliasConfig, GateResult, Receipt, Scores, Seal

# States after which the orchestrator waits for a person (or has stopped).
TERMINAL = {"awaiting_signoff", "blocked", "failed", "approved", "promoted", "rolled_back"}


@dataclass
class Labels:
    """What the output must say about where the numbers came from."""

    synthetic: bool = False
    executor: str | None = None  # runner, mock, replay, hf-jobs, together

    @property
    def words(self) -> list[str]:
        out = []
        if self.executor == "mock":
            out.append("MOCK executor: no training or inference ran; numbers are from fixture predictions")
        elif self.executor == "replay":
            out.append("REPLAY executor: predictions replayed from a recorded run")
        if self.synthetic:
            out.append("SYNTHETIC data")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"synthetic": self.synthetic, "executor": self.executor,
                "mock": self.executor == "mock", "replay": self.executor == "replay"}


def labels(view: dict[str, Any], receipt: Receipt | None) -> Labels:
    ex = receipt.predicate.executor.name if receipt is not None else None
    synthetic = bool(view.get("synthetic")) or bool(receipt is not None and receipt.predicate.synthetic)
    return Labels(synthetic=synthetic, executor=str(ex.value if hasattr(ex, "value") else ex) if ex else None)


def hhmmss(at: str) -> str:
    """'2026-11-01T10:14:00Z' -> '10:14:00' (the server's time, so output is reproducible)."""
    return at[11:19] if len(at) >= 19 else at


def event_text(e: dict[str, Any]) -> str:
    detail = e.get("detail")
    return f"{e['state']}  {detail}" if detail else str(e["state"])


@dataclass
class Followed:
    view: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    timed_out: bool = False


def follow(api: Api, first: dict[str, Any], on_event: Callable[[dict[str, Any]], None], *, interval: float,
           timeout: float, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> Followed:
    """Poll GET /v1/rebases/{n} and report each new timeline event once, until a terminal state or the timeout."""
    n = first["rebase"]
    seen = 0
    view = first
    out = Followed(view)
    start = clock()
    while True:
        tl = view.get("timeline") or []
        for e in tl[seen:]:
            out.events.append(e)
            on_event(e)
        seen = max(seen, len(tl))
        out.view = view
        if view.get("state") in TERMINAL:
            return out
        if clock() - start >= timeout:
            out.timed_out = True
            return out
        sleep(interval)
        view = api.get(f"/v1/rebases/{seg(n)}")


@dataclass
class Summary:
    lines: list[str]
    final: str
    status: str  # pass | blocked | failed | <state>
    labels: Labels
    scores: Scores | None
    gate: GateResult | None


def summarize(view: dict[str, Any]) -> Summary:
    n = view["rebase"]
    scores = Scores.model_validate(view["scores"]) if view.get("scores") else None
    gate = GateResult.model_validate(view["gate"]) if view.get("gate") else None
    receipt = Receipt.model_validate(view["receipt"]) if view.get("receipt") else None
    lab = labels(view, receipt)
    lines: list[str] = []
    if receipt is not None and lab.executor == "runner" and receipt.predicate.snapshot is not None:
        lines.append(f"trained on {receipt.predicate.snapshot.rows:,} verdicts")
    if scores is not None:
        o = scores.overall
        below = sum(s.below_margin for s in scores.slices)
        low = sum(s.low_n for s in scores.slices)
        lines.append(f"scored on {o.n:,} holdout cases")
        lines.append(f"{signed(o.delta)} (CI {signed(o.ci_low)} to {signed(o.ci_high)})")
        lines.append(f"{below} of {len(scores.slices)} slices below margin")
        if low:
            lines.append(f"{low} slices flagged low-n (they never block)")
    state = view["state"]
    if state == "failed":
        last = (view.get("timeline") or [{}])[-1]
        return Summary(lines, f"failed: rebase #{n} {last.get('detail') or 'stopped'}", "failed", lab, scores, gate)
    if gate is not None and gate.status == "blocked":
        margin = gate.findings[0].margin if gate.findings else 0.0
        return Summary(lines, final_line(n, gate, margin), "blocked", lab, scores, gate)
    if state in ("awaiting_signoff", "pr_opened") and gate is not None:
        return Summary(lines, final_line(n, gate, 0.0), "pass", lab, scores, gate)
    return Summary(lines, f"rebase #{n}: {state}", state, lab, scores, gate)


# ---------------------------------------------------------------- status


def model_lines(detail: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    s = detail["summary"]
    rev = s["base"].get("revision", "unpinned")
    out = [
        f"{s['model']}  {s['tenant']}" + ("  SYNTHETIC" if s.get("synthetic") else ""),
        f"alias  {s['alias']} {s['version']}",
        f"base   {s['base']['repo']} @{rev[:12] if rev != 'unpinned' else rev}",
        f"verdicts captured this week: {s['verdicts_this_week']:,}",
    ]
    return out, list(detail.get("recent_rebases") or [])


def rebase_row(r: dict[str, Any]) -> tuple[str, str, str, str]:
    d = r.get("delta")
    return f"#{r['rebase']}", r["state"], signed(d) if isinstance(d, int | float) else "", r.get("gate") or ""


# ---------------------------------------------------------------- seal, promote, rollback


def seal_line(seal: Seal) -> str:
    line = (f"sealed {seal.id} by {seal.sealed_by} at {seal.sealed_at} · audit seq {seal.audit_seq:,} · "
            f"{seal.rows:,} rows · sha256 {seal.sha256[:12]}")
    if seal.merkle_root:
        line += f" · merkle {seal.merkle_root[:12]}"
    return line


def alias_line(cfg: AliasConfig, alias: str, etag: str | None) -> str:
    route = cfg.aliases.get(alias)
    if route is None:
        raise ApiError(f"response has no alias {alias!r}")
    tail = f" (generation {cfg.generation}" + (f", etag {etag}" if etag else "") + ")"
    return f"{alias} → {route.version}{tail}"
