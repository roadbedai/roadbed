"""roadbed.scoring: weighted exact match, hand-computed."""

from __future__ import annotations

import pytest

from conftest import SCORERS
from roadbed.contracts import FieldWeight, Scorers
from roadbed.scoring import same_json, score_row


@pytest.mark.parametrize(
    "output,expected_score",
    [
        ({"route": "siu", "severity": 4}, 100.0),
        ({"route": "siu", "severity": 3}, 60.0),  # route weight 0.6 of 1.0
        ({"route": "standard", "severity": 4}, 40.0),
        ({"route": "siu", "severity": 4.0}, 100.0),  # JSON has one number type
        ({"route": "siu", "severity": "4"}, 60.0),
        ({"route": "siu"}, 60.0),  # a missing field scores 0
        ({}, 0.0),
    ],
)
def test_score_row_by_hand(output, expected_score):
    assert score_row({"route": "siu", "severity": 4}, output, SCORERS) == pytest.approx(expected_score)


def test_boolean_is_not_a_number():
    """Python has True == 1; JSON does not. A model that answers true for severity 1 is wrong."""
    sc = Scorers(type="weighted_field_exact_match", fields=[FieldWeight(field="severity", weight=1.0)])
    assert score_row({"severity": 1}, {"severity": True}, sc) == 0.0
    assert score_row({"severity": True}, {"severity": True}, sc) == 100.0
    assert not same_json({"a": [1, 0]}, {"a": [True, False]})
    assert same_json({"a": [1, {"b": None}]}, {"a": [1.0, {"b": None}]})
    assert not same_json({"a": 1}, {"a": 1, "b": 2})
