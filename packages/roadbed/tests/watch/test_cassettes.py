"""Committed cassettes hold no token, cookie, personal header or host outside the allow-list."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from hub_vcr import CASSETTES, KEEP_REQUEST_HEADERS, KEEP_RESPONSE_HEADERS
from ruamel.yaml import YAML

from roadbed.watch import ALLOWED_HOSTS

FILES = sorted(CASSETTES.glob("*.yaml"))
SECRET = re.compile(
    r"hf_[A-Za-z0-9]{20,}"            # Hugging Face tokens
    r"|gh[pousr]_[A-Za-z0-9]{20,}"     # GitHub tokens
    r"|-----BEGIN [A-Z ]*PRIVATE KEY"
    r"|(?i:bearer\s+[A-Za-z0-9._-]{8,})"
    r"|(?i:x-amz-[a-z-]+|x-request-id|set-cookie|\bcookie\b|authorization)"
    r"|/Users/|/home/",
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def test_cassettes_exist():
    names = {f.name for f in FILES}
    assert {"hub_list_qwen.yaml", "hub_scan_qwen3_27b.yaml", "hub_scan_gemma.yaml", "hub_scan_no_license.yaml"} <= names


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_cassette_has_no_secrets(path: Path):
    text = path.read_text(encoding="utf-8")
    assert not SECRET.search(text), SECRET.search(text)
    assert not EMAIL.findall(text)
    cas = YAML(typ="safe", pure=True).load(text)
    for i in cas["interactions"]:
        req, resp = i["request"], i["response"]
        assert {k.lower() for k in req["headers"]} <= KEEP_REQUEST_HEADERS
        assert {k.lower() for k in resp["headers"]} <= KEEP_RESPONSE_HEADERS
        url = urlsplit(req["uri"])
        assert url.scheme == "https" and url.hostname in ALLOWED_HOSTS
        assert req["method"] == "GET"
