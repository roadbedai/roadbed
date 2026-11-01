"""Watch the Hugging Face Hub for new base models that match a repo-id glob.

scan() lists the author's models on the Hub, keeps the repo ids that match the glob, pins each new
one to its current revision sha, reads its license (cardData and the LICENSE file at that sha) and
applies policy/license.yaml. A state file makes each base a detection only once. A 24 h cache of
the Hub answers lets a scan run offline.

Security: the only outbound host is huggingface.co over https. No token, cookie or proxy setting
is sent (trust_env=False, no Authorization header). Redirects are not followed. Every value read
from the Hub, the cache or the state file is checked against strict patterns before use.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from roadbed import __version__
from roadbed.contracts import LicensePolicy
from roadbed.license_policy import LicenseDecision, ModelCard, decide

HUB_URL = "https://huggingface.co"
ALLOWED_HOSTS = frozenset({"huggingface.co"})
CACHE_TTL = timedelta(hours=24)
CLOCK_SKEW = timedelta(minutes=5)
LIST_LIMIT = 500
MAX_NEW = 10  # most new bases fetched per scan; the rest wait for the next scan
MAX_JSON_BYTES = 4 << 20
MAX_LICENSE_BYTES = 256 << 10
LICENSE_FILES = ("LICENSE", "LICENSE.txt", "LICENSE.md")
CARD_FIELDS = ("license", "license_name")
TIMEOUT_S = 15.0

_SEG = r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}"
REPO_ID = re.compile(rf"^{_SEG}/{_SEG}$")
GLOB = re.compile(rf"^({_SEG})/([A-Za-z0-9._*?\[\]!-]{{1,96}})$")
SHA = re.compile(r"^[0-9a-f]{40}$")
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

Source = Literal["hub", "cache"]


class WatchError(Exception):
    pass


class BadGlob(WatchError, ValueError):
    pass


class NoMatch(WatchError):
    pass


class HubUnavailable(WatchError):
    """Network failure, timeout, 429 or 5xx: the cache may stand in."""


class HubError(WatchError):
    """The Hub answered with something we do not accept."""


class CacheMiss(WatchError):
    pass


class StateError(WatchError):
    pass


# ---------------------------------------------------------------- helpers


def ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def valid_repo(repo: Any) -> bool:
    return isinstance(repo, str) and bool(REPO_ID.fullmatch(repo)) and ".." not in repo


def parse_glob(glob: str) -> tuple[str, str]:
    """Split 'Author/name-glob'. The author is literal; wildcards are allowed in the name only."""
    m = GLOB.fullmatch(glob) if isinstance(glob, str) else None
    if not m or ".." in glob:
        raise BadGlob(f"base glob must look like 'Author/Name*' (letters, digits, . _ - and * ? [ ] ! in the name): {glob!r}")
    return m.group(1), m.group(2)


def matches(glob: str, repo_ids: list[str]) -> list[str]:
    """Repo ids that match glob, case-sensitive, in input order."""
    parse_glob(glob)
    return [r for r in repo_ids if valid_repo(r) and fnmatch.fnmatchcase(r, glob)]


def search_prefix(pattern: str) -> str:
    """Literal prefix of a name glob for the Hub substring search: 'Qwen3.*' -> 'Qwen3'."""
    out = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
    return out.rstrip(".-_")


def default_cache_dir() -> Path:
    base = os.environ.get("ROADBED_CACHE_DIR")
    return Path(base) if base else Path.home() / ".cache" / "roadbed"


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- stored shapes


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Listed(_Strict):
    id: str = Field(pattern=REPO_ID.pattern)
    created_at: str = Field(pattern=TS.pattern)


class Listing(_Strict):
    fetched_at: str = Field(pattern=TS.pattern)
    models: list[Listed]


class CachedCard(_Strict):
    fetched_at: str = Field(pattern=TS.pattern)
    revision: str = Field(pattern=SHA.pattern)
    card_data: dict[str, str | list[str]] = {}
    license_file: str | None = Field(default=None, max_length=MAX_LICENSE_BYTES)


class HubCache(_Strict):
    version: Literal[1] = 1
    listings: dict[str, Listing] = {}
    cards: dict[str, CachedCard] = {}


class DetectedBase(_Strict):
    revision: str = Field(pattern=SHA.pattern)
    glob: str
    detected_at: str = Field(pattern=TS.pattern)
    license: str
    decision: Literal["allow", "review", "deny"] | None


class WatchState(_Strict):
    version: Literal[1] = 1
    globs: list[str] = []
    seen: list[str] = []
    detected: dict[str, DetectedBase] = {}


def _load_cache(path: Path) -> HubCache:
    """A missing, unreadable or malformed cache is an empty cache (it is only a cache)."""
    try:
        cache = HubCache.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError):
        return HubCache()
    if not all(valid_repo(r) for r in cache.cards) or not all(valid_repo(m.id) for lst in cache.listings.values() for m in lst.models):
        return HubCache()
    return cache


def load_state(path: str | Path) -> WatchState:
    """A missing state file is a first scan. A malformed one is an error, not a reset."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return WatchState()
    try:
        state = WatchState.model_validate_json(raw)
    except (ValidationError, ValueError) as e:
        raise StateError(f"watch state {p} is not valid: {e.__class__.__name__}") from None
    if not all(valid_repo(r) for r in [*state.seen, *state.detected]):
        raise StateError(f"watch state {p} names an invalid repo id")
    return state


# ---------------------------------------------------------------- Hub


def hub_client() -> httpx.Client:
    """No auth, no cookies from env, no proxies from env, no redirects."""
    return httpx.Client(
        base_url=HUB_URL,
        timeout=TIMEOUT_S,
        follow_redirects=False,
        trust_env=False,
        headers={"user-agent": f"roadbed/{__version__}", "accept": "application/json, text/plain"},
    )


def _get(client: httpx.Client, path: str, params: dict[str, Any] | None, max_bytes: int) -> bytes:
    req = client.build_request("GET", path, params=params)
    if req.url.scheme != "https" or req.url.host not in ALLOWED_HOSTS:
        raise HubError(f"refusing request outside the allow-list: {req.url.host}")
    for h in ("authorization", "cookie"):
        req.headers.pop(h, None)
    try:
        resp = client.send(req, stream=True)
    except httpx.TransportError as e:
        raise HubUnavailable(f"GET {path}: {e.__class__.__name__}") from None
    try:
        if resp.status_code == 429 or resp.status_code >= 500:
            raise HubUnavailable(f"GET {path}: HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise HubError(f"GET {path}: HTTP {resp.status_code}")
        body = bytearray()
        try:
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise HubError(f"GET {path}: response larger than {max_bytes} bytes")
        except httpx.TransportError as e:
            raise HubUnavailable(f"GET {path}: {e.__class__.__name__}") from None
        return bytes(body)
    finally:
        resp.close()


def list_models(client: httpx.Client, glob: str, now: datetime) -> Listing:
    """The author's models whose id contains the glob's literal prefix, newest first."""
    author, pattern = parse_glob(glob)
    params: dict[str, Any] = {"author": author, "sort": "createdAt", "direction": "-1",
                              "limit": str(LIST_LIMIT), "expand[]": "createdAt"}
    prefix = search_prefix(pattern)
    if prefix:
        params["search"] = prefix
    data = json.loads(_get(client, "/api/models", params, MAX_JSON_BYTES))
    if not isinstance(data, list):
        raise HubError("model list is not a JSON array")
    models = []
    for item in data:
        if not isinstance(item, dict) or not valid_repo(item.get("id")) or not isinstance(item.get("createdAt"), str):
            continue  # skip, never trust, malformed rows
        try:
            created = ts(_parse_ts(item["createdAt"]))
        except ValueError:
            continue
        models.append(Listed(id=item["id"], created_at=created))
    return Listing(fetched_at=ts(now), models=models)


def fetch_card(client: httpx.Client, repo: str, now: datetime) -> CachedCard:
    """Pin repo to its current revision sha and read the license fields and LICENSE at that sha."""
    if not valid_repo(repo):
        raise HubError(f"invalid repo id {repo!r}")
    path = "/api/models/" + quote(repo, safe="/") + "/revision/main"
    info = json.loads(_get(client, path, {"expand[]": ["sha", "cardData", "siblings", "gated"]}, MAX_JSON_BYTES))
    if not isinstance(info, dict) or info.get("id") != repo:
        raise HubError(f"{repo}: model info does not name this repo (renamed or moved?)")
    sha = info.get("sha")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise HubError(f"{repo}: revision is not a 40-hex sha")
    raw = info.get("cardData")
    raw_card: dict[str, Any] = raw if isinstance(raw, dict) else {}
    card_data: dict[str, str | list[str]] = {}
    for k in CARD_FIELDS:
        v = raw_card.get(k)
        if isinstance(v, str):
            card_data[k] = v[:256]
        elif isinstance(v, list) and all(isinstance(x, str) for x in v):
            card_data[k] = [x[:256] for x in v[:8]]
    siblings = {s.get("rfilename") for s in info.get("siblings") or [] if isinstance(s, dict)}
    license_file = None
    if info.get("gated") is False:  # a gated repo needs a token for files; we never send one
        name = next((n for n in LICENSE_FILES if n in siblings), None)
        if name:
            body = _get(client, f"/{quote(repo, safe='/')}/raw/{sha}/{name}", None, MAX_LICENSE_BYTES)
            license_file = body.decode("utf-8", errors="replace")
    return CachedCard(fetched_at=ts(now), revision=sha, card_data=card_data, license_file=license_file)


# ---------------------------------------------------------------- scan


@dataclass(frozen=True)
class Detection:
    glob: str
    repo: str
    revision: str
    created_at: str
    license: str
    license_source: Literal["cardData", "LICENSE", "none"]
    decision: LicenseDecision | None  # None when no policy was given
    source: Source  # where the Hub data came from
    fetched_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_NO_POLICY = LicensePolicy(allow=[])


def _fresh(fetched_at: str, now: datetime) -> bool:
    """At most 24 h old. A timestamp more than 5 min in the future is not fresh (a forged cache entry)."""
    age = now - _parse_ts(fetched_at)
    return -CLOCK_SKEW <= age <= CACHE_TTL


def scan(glob: str, state_path: str | Path, policy: LicensePolicy | None, *, offline: bool = False,
         cache_dir: str | Path | None = None, now: datetime | None = None,
         client: httpx.Client | None = None) -> list[Detection]:
    """New bases matching glob, newest first. Updates the state file and the cache.

    First scan of a glob: the newest match is the detection and every other current match is
    recorded as seen. Later scans: every match not seen before is a detection (at most MAX_NEW).
    offline=True, or a Hub outage, reads the cache instead, if it is at most 24 h old.
    Raises BadGlob, NoMatch, CacheMiss, HubError, HubUnavailable or StateError.
    """
    parse_glob(glob)
    now = now or datetime.now(UTC)
    state_file = Path(state_path)
    cache_file = Path(cache_dir) if cache_dir else default_cache_dir()
    cache_file = cache_file / "hub-cache.json"
    state = load_state(state_file)
    cache = _load_cache(cache_file)
    listings, cards = dict(cache.listings), dict(cache.cards)

    http = None if offline else (client or hub_client())
    try:
        listing: Listing | None = None
        source: Source = "hub"
        if http is not None:
            try:
                listing = list_models(http, glob, now)
                listings[glob] = listing
            except HubUnavailable:
                listing = None
        if listing is None:
            cached = listings.get(glob)
            if cached is None or not _fresh(cached.fetched_at, now):
                why = "offline" if offline else "Hub unreachable"
                raise CacheMiss(f"{why} and no cached Hub listing under 24 h old for {glob}")
            listing, source = cached, "cache"

        found = sorted((m for m in listing.models if fnmatch.fnmatchcase(m.id, glob)),
                       key=lambda m: (m.created_at, m.id), reverse=True)
        if not found:
            raise NoMatch(glob)
        first = glob not in state.globs
        known = set(state.seen) | set(state.detected)
        new = [m for m in found if m.id not in known]
        todo = new[:1] if first else new[:MAX_NEW]

        detections = []
        for m in todo:
            if source == "hub" and http is not None:
                card = fetch_card(http, m.id, now)
                cards[m.id] = card
            else:
                c = cards.get(m.id)
                if c is None or not _fresh(c.fetched_at, now):
                    raise CacheMiss(f"no cached card under 24 h old for {m.id}")
                card = c
            lic = decide(ModelCard(repo=m.id, revision=card.revision, card_data=card.card_data,
                                   license_file=card.license_file), policy or _NO_POLICY)
            detections.append(Detection(
                glob=glob, repo=m.id, revision=card.revision, created_at=m.created_at, license=lic.license,
                license_source=lic.source, decision=lic if policy else None, source=source,
                fetched_at=card.fetched_at,
            ))
    finally:
        if http is not None and client is None:
            http.close()

    detected = dict(state.detected)
    for d in detections:
        detected[d.repo] = DetectedBase(revision=d.revision, glob=glob, detected_at=ts(now), license=d.license,
                                        decision=d.decision.decision if d.decision else None)
    seen = set(state.seen)
    if first:
        seen |= {m.id for m in found if m.id not in detected}
    new_state = WatchState(globs=sorted({*state.globs, glob}), seen=sorted(seen), detected=detected)
    # Drop cache entries past the 24 h window so the file does not grow without bound.
    listings = {g: v for g, v in listings.items() if _fresh(v.fetched_at, now)}
    cards = {r: v for r, v in cards.items() if _fresh(v.fetched_at, now)}
    write_json_atomic(cache_file, HubCache(listings=listings, cards=cards).model_dump(mode="json"))
    write_json_atomic(state_file, new_state.model_dump(mode="json"))
    return detections
