"""Label resolver: interactions, verdicts and late outcomes -> resolved rows under pinned label rules.

    from roadbed.labels import resolve, LABEL_RULES_V1
    rows = resolve(interactions, verdicts, outcomes, LABEL_RULES_V1, as_of="2026-11-01T09:00:00Z")
"""

from roadbed.labels.resolve import ResolveError, resolve, summary
from roadbed.labels.rules import LABEL_RULES_V1, PINNED, LabelRules, RulesNotPinned, check_pinned, weight

__all__ = ["LABEL_RULES_V1", "PINNED", "LabelRules", "ResolveError", "RulesNotPinned", "check_pinned", "resolve", "summary", "weight"]
