"""roadbed.license_policy: known-answer tables over decide(), policy loading and the license.json record."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from hub_vcr import CASSETTES

from roadbed.contracts import LicensePolicy, PinnedBase, json_schema
from roadbed.license_policy import ModelCard, decide, license_from_text, load_policy, policy_sha256

SPEC = Path(__file__).resolve().parents[4] / "spec"
PASS_POLICY = SPEC / "examples" / "pass" / "policy" / "license.yaml"
POLICY = load_policy(PASS_POLICY)
REV = "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"


def card(card_data=None, license_file=None) -> ModelCard:
    return ModelCard(repo="synthetic/base", revision=REV, card_data=card_data, license_file=license_file)


@pytest.mark.parametrize("card_data, want_license, want_decision", [
    ({"license": "apache-2.0"}, "apache-2.0", "allow"),
    ({"license": "Apache-2.0"}, "apache-2.0", "allow"),
    ({"license": "mit"}, "mit", "allow"),
    ({"license": "gemma"}, "gemma", "review"),
    ({"license": "llama3.2"}, "llama3.2", "review"),
    ({"license": "llama"}, "llama", "review"),
    ({"license": "cc-by-nc-4.0"}, "cc-by-nc-4.0", "deny"),
    ({"license": "other", "license_name": "qwen-research"}, "qwen-research", "deny"),
    ({"license": "other"}, "other", "deny"),
    ({"license": ["apache-2.0", "gemma"]}, "apache-2.0,gemma", "review"),
    ({"license": ["apache-2.0", "cc-by-nc-4.0"]}, "apache-2.0,cc-by-nc-4.0", "deny"),
    ({"license": "apache-2.0 OR evil*"}, "invalid", "deny"),
    ({"license": 7}, "invalid", "deny"),
    ({"license": ""}, "none", "deny"),
    ({}, "none", "deny"),
    (None, "none", "deny"),
])
def test_decide_from_card(card_data, want_license, want_decision):
    d = decide(card(card_data), POLICY)
    assert (d.license, d.decision) == (want_license, want_decision)


def test_missing_license_denied_even_when_default_is_review():
    lenient = LicensePolicy(allow=["apache-2.0"], default="review")
    d = decide(card(None, None), lenient)
    assert (d.license, d.source, d.decision) == ("none", "none", "deny")
    assert decide(card({"license": "cc-by-4.0"}), lenient).decision == "review"


def test_review_glob_does_not_widen_allow():
    p = LicensePolicy(allow=["apache-2.0"], review=["llama*"])
    assert decide(card({"license": "apache-2.0-modified"}), p).decision == "deny"
    assert decide(card({"license": "llama3.1"}), p).decision == "review"


def _recorded_license_text() -> str:
    """The real Qwen3.8-27B LICENSE body from the recorded cassette."""
    from ruamel.yaml import YAML

    cas = YAML(typ="safe", pure=True).load((CASSETTES / "hub_scan_qwen3_27b.yaml").read_text())
    for i in cas["interactions"]:
        if i["request"]["uri"].endswith("/LICENSE"):
            return i["response"]["body"]["string"]
    raise AssertionError("no LICENSE in cassette")


def test_license_file_is_the_fallback_source():
    text = _recorded_license_text()
    assert license_from_text(text) == "apache-2.0"
    d = decide(card(None, text), POLICY)
    assert (d.license, d.source, d.decision) == ("apache-2.0", "LICENSE", "allow")


def test_card_and_license_file_disagree_means_review():
    mit = ("MIT License\n\nPermission is hereby granted, free of charge, to any person ... "
           "The above copyright notice and this permission notice shall be included in all copies")
    d = decide(card({"license": "apache-2.0"}, mit), POLICY)
    assert (d.source, d.decision) == ("cardData", "review")


@pytest.mark.parametrize("text, want", [
    ("", None),
    ("   \n", None),
    ("Gemma Terms of Use\nLast modified: ...", "gemma"),
    ("LLAMA 3.2 COMMUNITY LICENSE AGREEMENT", "llama3.2"),
    ("All rights reserved.", "other"),
])
def test_license_from_text(text, want):
    assert license_from_text(text) == want


def test_policy_sha256_matches_fixture():
    rec = json.loads((SPEC / "examples" / "pass" / "rebases" / "13" / "license.json").read_text())
    assert policy_sha256(POLICY) == rec["policy_sha256"] == "1074efbae008522a295b4e99e11c11fca3388e80d40fc947fc2d8a1680f63ed5"


def test_record_reproduces_fixture_license_json():
    want = json.loads((SPEC / "examples" / "pass" / "rebases" / "13" / "license.json").read_text())
    d = decide(card({"license": "apache-2.0"}), POLICY)
    rec = d.record(rebase=13, base=PinnedBase(repo="Qwen/Qwen3.8-4B", revision=REV), policy=POLICY,
                   decided_at="2026-11-01T10:14:30Z", synthetic=True)
    got = rec.model_dump(mode="json", exclude_none=True)
    jsonschema.Draft202012Validator(json_schema("license-decision")).validate(got)
    assert got == want


def test_load_policy_rejects_unknown_keys_and_unsafe_yaml(tmp_path: Path):
    p = tmp_path / "license.yaml"
    p.write_text("allow: [apache-2.0]\ndeny_none: true\n")
    with pytest.raises(jsonschema.ValidationError):
        load_policy(p)
    marker = tmp_path / "pwned"
    p.write_text(f"allow: !!python/object/apply:os.system ['touch {marker}']\n")
    with pytest.raises(Exception):  # noqa: B017  (ruamel safe loader refuses the tag)
        load_policy(p)
    assert not marker.exists()
    p.write_text("allow: apache-2.0\n")
    with pytest.raises(jsonschema.ValidationError):
        load_policy(p)
