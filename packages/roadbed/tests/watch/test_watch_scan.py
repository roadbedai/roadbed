"""roadbed.watch.scan against recorded Hub answers (cassettes) and a few fake transports.

Cassettes were recorded once against the real huggingface.co (see hub_vcr.py). Fake transports
(httpx.MockTransport) only stand in for Hub states we cannot record on demand: a repo that appears
between two scans, an outage, a redirect. Their repo ids say "synthetic".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from hub_vcr import cassette

from roadbed import watch
from roadbed.license_policy import load_policy

SPEC = Path(__file__).resolve().parents[4] / "spec"
POLICY = load_policy(SPEC / "examples" / "pass" / "policy" / "license.yaml")  # allow apache-2.0, mit; review gemma, llama*
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
QWEN38_SHA = "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "state.json", tmp_path / "cache"


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a, **kw):
        raise AssertionError("network used in an offline scan")

    monkeypatch.setattr(httpx.Client, "send", refuse)


# ---------------------------------------------------------------- glob


def test_glob_matches_qwen38_not_qwen25():
    with cassette("hub_list_qwen.yaml"), watch.hub_client() as c:
        listing = watch.list_models(c, "Qwen/Qwen*", NOW)  # every Qwen/Qwen* repo on the Hub
    ids = [m.id for m in listing.models]
    qwen25 = [i for i in ids if i.startswith("Qwen/Qwen2.5")]
    assert "Qwen/Qwen3.8-27B" in ids and len(qwen25) > 50  # the recorded listing holds both families
    hit = watch.matches("Qwen/Qwen3.*", ids)
    assert "Qwen/Qwen3.8-27B" in hit
    assert not [i for i in hit if not i.startswith("Qwen/Qwen3.")]
    assert not set(hit) & set(qwen25)


@pytest.mark.parametrize("glob", [
    "Qwen", "*/Qwen3", "Qw*n/Qwen3", "Qwen/../x", "../Qwen/x", "Qwen/a/b", "Qwen/Qwen3;rm -rf", "Qwen/$(id)",
    "Qwen/Qwen 3", "Qwen/", "/Qwen3", "Qwen/Qwen3\n", "Qwen/" + "a" * 97, "https://evil.example/Qwen",
])
def test_bad_globs_rejected(glob: str, dirs):
    with pytest.raises(watch.BadGlob):
        watch.scan(glob, dirs[0], POLICY, cache_dir=dirs[1], offline=True)


def test_search_prefix():
    assert watch.search_prefix("Qwen3.*") == "Qwen3"
    assert watch.search_prefix("Qwen3.*-27B") == "Qwen3"
    assert watch.search_prefix("gemma-3-1b-it") == "gemma-3-1b-it"
    assert watch.search_prefix("*") == ""


# ---------------------------------------------------------------- detection and license


def test_qwen38_detected_once_pinned_and_allowed(dirs):
    state, cache = dirs
    with cassette("hub_scan_qwen3_27b.yaml") as cas:
        first = watch.scan("Qwen/Qwen3.*-27B", state, POLICY, cache_dir=cache, now=NOW)
        second = watch.scan("Qwen/Qwen3.*-27B", state, POLICY, cache_dir=cache, now=NOW + timedelta(hours=1))
        assert cas.play_count >= 3  # list, card, LICENSE: all replayed from the recording
    assert [(d.repo, d.revision) for d in first] == [("Qwen/Qwen3.8-27B", QWEN38_SHA)]
    d = first[0]
    assert d.license == "apache-2.0" and d.license_source == "cardData" and d.source == "hub"
    assert d.decision is not None and d.decision.decision == "allow"
    assert second == []
    s = json.loads(state.read_text())
    assert s["detected"]["Qwen/Qwen3.8-27B"]["revision"] == QWEN38_SHA
    assert "Qwen/Qwen3.6-27B" in s["seen"]  # older matches become the baseline, not detections


def test_gemma_needs_review(dirs):
    with cassette("hub_scan_gemma.yaml"):
        [d] = watch.scan("google/gemma-3-1b-it", dirs[0], POLICY, cache_dir=dirs[1], now=NOW)
    assert d.license == "gemma" and d.license_source == "cardData"
    assert d.decision is not None and d.decision.decision == "review"


def test_missing_license_denied(dirs):
    with cassette("hub_scan_no_license.yaml"):
        [d] = watch.scan("sshleifer/tiny-gpt2", dirs[0], POLICY, cache_dir=dirs[1], now=NOW)
    assert d.license == "none" and d.license_source == "none"
    assert d.decision is not None and d.decision.decision == "deny"


def test_no_match(dirs):
    with cassette("hub_scan_qwen3_27b.yaml"), pytest.raises(watch.NoMatch):
        watch.scan("Qwen/Qwen3.*-99B", dirs[0], POLICY, cache_dir=dirs[1], now=NOW)


# ---------------------------------------------------------------- offline cache


def test_offline_run_from_cache(dirs, tmp_path: Path, monkeypatch):
    state, cache = dirs
    with cassette("hub_scan_qwen3_27b.yaml"):
        online = watch.scan("Qwen/Qwen3.*-27B", state, POLICY, cache_dir=cache, now=NOW)
    _no_network(monkeypatch)
    offline = watch.scan("Qwen/Qwen3.*-27B", tmp_path / "other-state.json", POLICY, cache_dir=cache,
                         now=NOW + timedelta(hours=23), offline=True)
    assert [(d.repo, d.revision, d.license) for d in offline] == [(d.repo, d.revision, d.license) for d in online]
    assert offline[0].source == "cache" and offline[0].decision.decision == "allow"


def test_offline_cache_expires_after_24h(dirs, tmp_path: Path, monkeypatch):
    state, cache = dirs
    with cassette("hub_scan_qwen3_27b.yaml"):
        watch.scan("Qwen/Qwen3.*-27B", state, POLICY, cache_dir=cache, now=NOW)
    _no_network(monkeypatch)
    with pytest.raises(watch.CacheMiss):
        watch.scan("Qwen/Qwen3.*-27B", tmp_path / "s2.json", POLICY, cache_dir=cache,
                   now=NOW + timedelta(hours=24, seconds=1), offline=True)


def test_offline_without_cache(dirs, monkeypatch):
    _no_network(monkeypatch)
    with pytest.raises(watch.CacheMiss):
        watch.scan("Qwen/Qwen3.*-27B", dirs[0], POLICY, cache_dir=dirs[1], offline=True, now=NOW)


def test_hub_outage_falls_back_to_cache(dirs, tmp_path: Path):
    state, cache = dirs
    with cassette("hub_scan_qwen3_27b.yaml"):
        watch.scan("Qwen/Qwen3.*-27B", state, POLICY, cache_dir=cache, now=NOW)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = httpx.Client(base_url=watch.HUB_URL, transport=httpx.MockTransport(down))
    [d] = watch.scan("Qwen/Qwen3.*-27B", tmp_path / "s2.json", POLICY, cache_dir=cache, now=NOW, client=client)
    assert d.source == "cache" and d.revision == QWEN38_SHA


# ---------------------------------------------------------------- state transitions (fake Hub)

SYN_SHA = "a" * 40


def _fake_hub(models: list[dict], sha: str = SYN_SHA) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/models":
            return httpx.Response(200, json=models)
        repo = request.url.path.removeprefix("/api/models/").removesuffix("/revision/main")
        return httpx.Response(200, json={"id": repo, "sha": sha, "gated": False, "siblings": [],
                                         "cardData": {"license": "mit"}})

    return httpx.Client(base_url=watch.HUB_URL, transport=httpx.MockTransport(handler))


def test_new_repo_between_scans_detected_once(dirs):
    state, cache = dirs
    a = {"id": "synthetic/base-1", "createdAt": "2026-10-01T00:00:00.000Z"}
    b = {"id": "synthetic/base-2", "createdAt": "2026-10-20T00:00:00.000Z"}
    old = {"id": "synthetic/base-0", "createdAt": "2026-01-01T00:00:00.000Z"}
    assert [d.repo for d in watch.scan("synthetic/base-*", state, POLICY, cache_dir=cache, now=NOW,
                                       client=_fake_hub([a, old]))] == ["synthetic/base-1"]
    assert [d.repo for d in watch.scan("synthetic/base-*", state, POLICY, cache_dir=cache, now=NOW,
                                       client=_fake_hub([b, a, old]))] == ["synthetic/base-2"]
    assert watch.scan("synthetic/base-*", state, POLICY, cache_dir=cache, now=NOW, client=_fake_hub([b, a, old])) == []


def test_hub_rows_with_bad_ids_are_ignored(dirs):
    rows = [{"id": "synthetic/../../etc", "createdAt": "2026-10-30T00:00:00Z"},
            {"id": "synthetic/x;rm", "createdAt": "2026-10-30T00:00:00Z"},
            {"id": "synthetic/base-1", "createdAt": "2026-10-01T00:00:00Z"}]
    [d] = watch.scan("synthetic/*", dirs[0], POLICY, cache_dir=dirs[1], now=NOW, client=_fake_hub(rows))
    assert d.repo == "synthetic/base-1"


def test_bad_sha_from_hub_rejected(dirs):
    with pytest.raises(watch.HubError):
        watch.scan("synthetic/base-*", dirs[0], POLICY, cache_dir=dirs[1], now=NOW,
                   client=_fake_hub([{"id": "synthetic/base-1", "createdAt": "2026-10-01T00:00:00Z"}], sha="main"))


def test_redirects_not_followed(dirs):
    client = httpx.Client(base_url=watch.HUB_URL, transport=httpx.MockTransport(
        lambda r: httpx.Response(302, headers={"location": "https://evil.example/api/models"})))
    with pytest.raises(watch.HubError):
        watch.scan("synthetic/base-*", dirs[0], POLICY, cache_dir=dirs[1], now=NOW, client=client)


def test_host_outside_allow_list_refused(dirs):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=[])

    client = httpx.Client(base_url="https://evil.example", transport=httpx.MockTransport(handler))
    with pytest.raises(watch.HubError):
        watch.scan("synthetic/base-*", dirs[0], POLICY, cache_dir=dirs[1], now=NOW, client=client)
    assert seen == []


def test_no_credentials_sent(dirs, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_SYNTHETICtokenSYNTHETICtoken0000")
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=[])

    client = watch.hub_client()
    client._transport = httpx.MockTransport(handler)  # keep hub_client()'s headers and settings
    with pytest.raises(watch.NoMatch):
        watch.scan("synthetic/base-*", dirs[0], POLICY, cache_dir=dirs[1], now=NOW, client=client)
    assert sent and all(k.lower() not in {"authorization", "cookie"} for r in sent for k in r.headers)
    assert client.trust_env is False and client.follow_redirects is False


# ---------------------------------------------------------------- tampered state and cache


def test_tampered_state_is_an_error_not_a_reset(dirs):
    state, cache = dirs
    state.write_text(json.dumps({"version": 1, "globs": [], "seen": ["../../x"], "detected": {}}))
    with pytest.raises(watch.StateError):
        watch.scan("Qwen/Qwen3.*-27B", state, POLICY, cache_dir=cache, offline=True, now=NOW)
    state.write_text("{not json")
    with pytest.raises(watch.StateError):
        watch.scan("Qwen/Qwen3.*-27B", state, POLICY, cache_dir=cache, offline=True, now=NOW)


def test_poisoned_cache_is_ignored(dirs, tmp_path: Path, monkeypatch):
    state, cache = dirs
    with cassette("hub_scan_qwen3_27b.yaml"):
        watch.scan("Qwen/Qwen3.*-27B", state, POLICY, cache_dir=cache, now=NOW)
    f = cache / "hub-cache.json"
    data = json.loads(f.read_text())
    data["cards"]["Qwen/Qwen3.8-27B"]["revision"] = "../../../../etc/passwd"
    f.write_text(json.dumps(data))
    _no_network(monkeypatch)
    with pytest.raises(watch.CacheMiss):
        watch.scan("Qwen/Qwen3.*-27B", tmp_path / "s2.json", POLICY, cache_dir=cache, offline=True, now=NOW)


def test_cache_entry_from_the_future_is_not_fresh(dirs, tmp_path: Path, monkeypatch):
    state, cache = dirs
    with cassette("hub_scan_qwen3_27b.yaml"):
        watch.scan("Qwen/Qwen3.*-27B", state, POLICY, cache_dir=cache, now=NOW + timedelta(days=365))
    _no_network(monkeypatch)
    with pytest.raises(watch.CacheMiss):
        watch.scan("Qwen/Qwen3.*-27B", tmp_path / "s2.json", POLICY, cache_dir=cache, offline=True, now=NOW)
