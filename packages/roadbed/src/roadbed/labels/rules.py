"""label_rules@v1: how a captured interaction, its reviewer verdicts and its late outcomes become one label.

The rules are data. Their sha256(JCS) is pinned below, so a changed rule set cannot run under the v1 name: the
resolver refuses rules whose content does not hash to the pin for their version. The version string goes into
every snapshot MANIFEST.json (`label_rules`).

v1, in words:

1. The latest verdict per interaction wins (by `ts`, then `verdict_id`). Events after `as_of` are not seen.
2. `approve` labels the row with the model output. `fix` labels it with the reviewer's corrected output.
3. `reject` and `escalate` give no label. Those rows are for evals only and never enter a training snapshot.
4. A late outcome overrides the verdict: the latest outcome (by `observed_at`, then `outcome_id`) observed at or
   after the verdict time replaces each label field it names. An outcome key `final_<field>` or `<field>` names
   `<field>`; keys that name no label field are kept on the row as `outcome` but change nothing. An outcome
   observed before the verdict does not override it: the reviewer decided later.
5. Weights: an edit (`fix`) weighs 2.0, an `approve` 1.0. A snapshot row has no weight field; under v1 the weight
   follows from the row itself (`reason_code` is set only on a fix), see `weight()`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from roadbed.hashing import sha256_canonical

ACTIONS = ("approve", "fix", "reject", "escalate")


@dataclass(frozen=True)
class LabelRules:
    version: str
    weights: Mapping[str, float]
    eval_only: tuple[str, ...]
    outcome_prefix: str = "final_"
    late_outcome_overrides: bool = True
    note: str = field(default="", compare=False)

    def as_json(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("note")
        d["weights"] = dict(sorted(self.weights.items()))
        d["eval_only"] = list(self.eval_only)
        return d

    @property
    def sha256(self) -> str:
        return sha256_canonical(self.as_json())

    def outcome_field(self, key: str) -> str:
        return key[len(self.outcome_prefix):] if self.outcome_prefix and key.startswith(self.outcome_prefix) else key


LABEL_RULES_V1 = LabelRules(
    version="label_rules@v1",
    weights={"approve": 1.0, "fix": 2.0, "reject": 0.0, "escalate": 0.0},
    eval_only=("reject", "escalate"),
    note="late outcome overrides; edit weighs 2.0; reject and escalate for evals only",
)

# version -> sha256(JCS(rules.as_json())). A rule change needs a new version and a new pin.
PINNED: dict[str, str] = {"label_rules@v1": "d02189564e0d31894f372817b313e70cc8b4117b98559aab4e2d7ed6193d6401"}


class RulesNotPinned(ValueError):
    pass


def check_pinned(rules: LabelRules) -> None:
    """Raise RulesNotPinned unless the rules hash to the pin of their version."""
    want = PINNED.get(rules.version)
    if want is None:
        raise RulesNotPinned(f"{rules.version!r} is not a pinned label rules version (known: {', '.join(sorted(PINNED))})")
    if rules.sha256 != want:
        raise RulesNotPinned(f"rules named {rules.version} hash to {rules.sha256[:12]}, not the pinned {want[:12]}")


def weight(row: Mapping[str, Any], rules: LabelRules = LABEL_RULES_V1) -> float:
    """Training weight of a resolved or snapshot row. A resolved row carries `action`; a snapshot row does not,
    and under v1 a non-null `reason_code` marks a fix (VerdictIn requires it on a fix and the resolver keeps it
    only for fixes)."""
    action = row.get("action")
    if action is None:
        action = "fix" if row.get("reason_code") is not None else "approve"
    return float(rules.weights[action])
