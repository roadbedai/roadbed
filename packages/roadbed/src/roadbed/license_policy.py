"""Base-model license policy: read the license of a pinned Hub revision and decide allow, review or deny.

The license comes from the model card metadata (cardData) and from the LICENSE file at the pinned
revision. policy/license.yaml lists allowed ids (exact match) and review ids or globs. Anything else
gets the policy default, which is deny when absent. A base with no readable license is always denied.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import jsonschema

from roadbed import SPEC_VERSION
from roadbed.contracts import LicenseDecisionRecord, LicensePolicy, PinnedBase, json_schema
from roadbed.hashing import sha256_canonical
from roadbed.io import read_yaml

Decision = Literal["allow", "review", "deny"]
Source = Literal["cardData", "LICENSE", "none"]

# Lowercase license id as the Hub writes it (SPDX ids and Hub ids such as gemma, llama3.2).
LICENSE_ID = re.compile(r"^[a-z0-9][a-z0-9.+_-]{0,63}$")
NO_LICENSE = "none"
INVALID = "invalid"


@dataclass(frozen=True)
class ModelCard:
    """What the Hub says about one pinned revision. Only license fields are read."""

    repo: str
    revision: str
    card_data: Mapping[str, Any] | None = None
    license_file: str | None = None  # text of LICENSE at the revision; None when there is none


@dataclass(frozen=True)
class LicenseDecision:
    license: str
    source: Source
    decision: Decision
    reason: str

    def record(self, *, rebase: int, base: PinnedBase, policy: LicensePolicy, decided_at: str,
               synthetic: bool, policy_path: str = "policy/license.yaml") -> LicenseDecisionRecord:
        """rebases/<n>/license.json for this decision."""
        return LicenseDecisionRecord(
            spec=SPEC_VERSION, rebase=rebase, base=base, license=self.license, source=self.source,
            decision=self.decision, policy=policy_path, policy_sha256=policy_sha256(policy),
            decided_at=decided_at, synthetic=synthetic,
        )


def policy_sha256(policy: LicensePolicy) -> str:
    """sha256(JCS(policy)) with null fields left out (SPEC.md)."""
    return sha256_canonical(policy.model_dump(mode="json", exclude_none=True))


def load_policy(path: str | Path) -> LicensePolicy:
    """Read policy/license.yaml (safe YAML) and validate it against the closed license-policy schema."""
    data = read_yaml(path)
    jsonschema.Draft202012Validator(json_schema("license-policy")).validate(data)
    return LicensePolicy.model_validate(data)


def _norm(value: Any) -> str | None:
    """Lowercase license id, INVALID for a value that is not one, None for empty."""
    if value is None:
        return None
    if not isinstance(value, str):
        return INVALID
    s = value.strip().lower()
    if not s:
        return None
    return s if LICENSE_ID.fullmatch(s) else INVALID


def _card_ids(card_data: Mapping[str, Any] | None) -> list[str]:
    """License ids in cardData. "other" with a license_name uses the name."""
    if not card_data:
        return []
    raw = card_data.get("license")
    values = raw if isinstance(raw, list) else [raw]
    ids = [i for i in (_norm(v) for v in values) if i]
    if "other" in ids:
        name = _norm(card_data.get("license_name"))
        if name:
            ids = [name if i == "other" else i for i in ids]
    return ids


_LLAMA = re.compile(r"llama (\d+(?:\.\d+)?) community license")


def license_from_text(text: str | None) -> str | None:
    """Known-answer classifier for LICENSE file text. None when empty, "other" when not recognised."""
    if not text or not text.strip():
        return None
    t = " ".join(text.lower().split())
    if "apache license" in t and "version 2.0" in t:
        return "apache-2.0"
    if "permission is hereby granted, free of charge" in t and "this permission notice shall be included" in t:
        return "mit"
    if "gemma terms of use" in t:
        return "gemma"
    m = _LLAMA.search(t)
    if m:
        return "llama" + m.group(1)
    return "other"


def _policy_decision(lic: str, policy: LicensePolicy) -> tuple[Decision, str]:
    if lic in {a.strip().lower() for a in policy.allow}:
        return "allow", "in allow list"
    for pat in policy.review or []:
        if fnmatch.fnmatchcase(lic, pat.strip().lower()):
            return "review", f"matches review entry {pat!r}"
    default: Decision = policy.default or "deny"
    return default, f"not listed; policy default {default}"


_RANK: dict[Decision, int] = {"allow": 0, "review": 1, "deny": 2}


def decide(card: ModelCard, policy: LicensePolicy) -> LicenseDecision:
    """Decide allow, review or deny for one pinned revision.

    cardData wins as the source when it names a license; the LICENSE file is the fallback.
    Several card ids: the strictest decision wins. A LICENSE file that names a different known
    license than the card: at least review. No license, or a malformed id: deny.
    """
    card_ids = _card_ids(card.card_data)
    file_id = license_from_text(card.license_file)

    if card_ids:
        source: Source = "cardData"
        ids = card_ids
    elif file_id:
        source, ids = "LICENSE", [file_id]
    else:
        return LicenseDecision(NO_LICENSE, "none", "deny", "no license in cardData or LICENSE")

    if INVALID in ids:
        return LicenseDecision(INVALID, source, "deny", "license value is not a license id")

    decision: Decision = "allow"
    reason = ""
    for lic in ids:
        d, why = _policy_decision(lic, policy)
        if _RANK[d] >= _RANK[decision]:
            decision, reason = d, f"{lic}: {why}"
    lic_str = ",".join(ids)
    if source == "cardData" and file_id and file_id != "other" and file_id not in ids and decision == "allow":
        decision, reason = "review", f"cardData says {lic_str} but LICENSE reads as {file_id}"
    return LicenseDecision(lic_str, source, decision, reason)
