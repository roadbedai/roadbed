"""Ids the SDK makes must never look like a phone or card number to the engine guard or the platform audit guard.

The engine quarantines an interaction whose id is contact-shaped (9 to 15 digits in a run). The platform refuses an id
with a run of 7 or more digits. So `new_id` redraws until the id has no such run.
"""

from __future__ import annotations

import base64
import re
from unittest import mock

import pytest

from conftest import TEST_KEY
from roadbed.contracts import InteractionIn, OutcomeIn, VerdictIn
from roadbed.labels.guard import contact_shaped
from roadbed_sdk import _runtime
from roadbed_sdk._pseudo import Pseudonymiser

ID_FORMAT = re.compile(r"^(ix|vd|oc)_[a-z2-7]{26}$")
SEVEN_DIGITS = re.compile(r"\d{7}")


def _urandom_giving(digits: int) -> bytes:
    """16 bytes whose base32 text starts with `digits` digit characters (2 to 7), then letters."""
    text = "2" * digits + "A" * (26 - digits)
    return base64.b32decode(text + "======")


def test_the_generator_redraws_while_the_id_holds_a_long_digit_run() -> None:
    draws = [_urandom_giving(9), _urandom_giving(7), _urandom_giving(6)]
    with mock.patch.object(_runtime.os, "urandom", side_effect=draws) as urandom:
        ix = _runtime.new_id("ix")
    assert urandom.call_count == 3
    assert ix == "ix_" + "2" * 6 + "a" * 20


def test_ten_thousand_ids_have_no_long_digit_run_and_fit_the_contracts() -> None:
    fields = {"ix": InteractionIn.model_fields["interaction_id"], "vd": VerdictIn.model_fields["verdict_id"],
              "oc": OutcomeIn.model_fields["outcome_id"]}
    p = Pseudonymiser(TEST_KEY)
    for n in range(10_000):
        prefix = ("ix", "vd", "oc")[n % 3]
        s = _runtime.new_id(prefix)
        assert ID_FORMAT.match(s) and s.startswith(prefix + "_"), s
        assert not SEVEN_DIGITS.search(s), s
        assert contact_shaped(s) is None, s
        assert p.safe_id(s), s
        meta = {type(m).__name__: m for m in fields[prefix].metadata}
        assert meta["MinLen"].min_length <= len(s) <= meta["MaxLen"].max_length


def test_ids_are_not_repeated() -> None:
    ids = {_runtime.new_id("ix") for _ in range(10_000)}
    assert len(ids) == 10_000


@pytest.mark.parametrize("digits", [7, 9, 12])
def test_an_id_from_the_generator_survives_a_bad_first_draw(digits: int) -> None:
    with mock.patch.object(_runtime.os, "urandom", side_effect=[_urandom_giving(digits), bytes(16)]):
        assert contact_shaped(_runtime.new_id("ix")) is None
