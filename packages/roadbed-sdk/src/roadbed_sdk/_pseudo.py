"""Client-side pseudonymisation. Nothing leaves this module with raw personal data in it.

Rules (applied to every string leaf of an input, output, verdict or outcome):

1. Values the host names as personal data (`pii=`) become placeholders such as `<PERSON_1>`.
2. Pattern entities (email, phone, IBAN, card, SSN, IP, date) become placeholders such as `<EMAIL_ADDRESS_1>`.
3. Free text then depends on the text mode:
   - "tokenize" (default, needs no extra): the whole string becomes `<TEXT:tok_...>`, an HMAC of the original.
   - "redact": a named-entity redactor (Presidio, the `[redact]` extra) replaces the entities it finds.
4. A string under a key in `keep` (labels such as `route`) keeps its value after rules 1 and 2 only if it looks like
   an identifier (no spaces, at most 64 characters). Anything else is tokenised.
5. A number with 7 or more integer digits (phone or account numbers) is tokenised unless its key is in `keep`.
6. A dict key that is not a field name (snake_case or camelCase, starting lowercase) is tokenised.
7. Ids from the host or the gateway must be plain ids without pattern entities, or they are not used.

Tokens are HMAC-SHA256 with the tenant key. Only the key id is recorded; the key never leaves the process.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

TextMode = Literal["tokenize", "redact"]

MIN_KEY_BYTES = 16
_KEY_ID_DOMAIN = b"roadbed-sdk/hmac-key-id/v1\x00"
_IDENT = re.compile(r"^[A-Za-z0-9_.:/+=-]{0,64}$")
# Field names: snake_case or camelCase that starts lowercase. A capitalised key ("Virtanen") is tokenised.
_FIELD = re.compile(r"^[a-z_][A-Za-z0-9_.-]{0,63}$")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_PLACEHOLDER = re.compile(r"^<[A-Z_]+_\d+>$")
_BIG_NUMBER = 1_000_000

# Order matters: longer, more specific patterns first. Kinds use Presidio entity names.
_L, _R = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"  # like \b, but an underscore does not hide an entity ("ix_123-45-6789")
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL_ADDRESS", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("IBAN_CODE", re.compile(_L + r"[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?" + _R)),
    ("US_SSN", re.compile(_L + r"\d{3}-\d{2}-\d{4}" + _R)),
    ("DATE_TIME", re.compile(_L + r"(?:\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?|\d{1,2}[./]\d{1,2}[./]\d{2,4})" + _R)),
    ("IP_ADDRESS", re.compile(_L + r"(?:\d{1,3}\.){3}\d{1,3}" + _R)),
    ("CREDIT_CARD", re.compile(_L + r"(?:\d[ -]?){12,18}\d" + _R)),
    ("PHONE_NUMBER", re.compile(r"(?<![A-Za-z0-9+])\+?\d(?:[\d ()./-]{5,}\d)" + _R)),
]


def _min_digits(kind: str) -> int:
    return 7 if kind == "PHONE_NUMBER" else 0


def key_bytes(tenant_key: str | bytes) -> bytes:
    """The tenant key as bytes. A str is used as UTF-8 (hex and base64 keys are fine as they are)."""
    return tenant_key.encode() if isinstance(tenant_key, str) else bytes(tenant_key)


def derive_key_id(key: bytes) -> str:
    """Public id of a tenant key: hk_ plus 16 hex of a domain-separated sha256. It does not reveal the key."""
    return "hk_" + hashlib.sha256(_KEY_ID_DOMAIN + key).hexdigest()[:16]


def hmac_hex(key: bytes, purpose: str, value: str) -> str:
    """HMAC-SHA256(key, purpose || 0x00 || value) as lowercase hex."""
    return hmac.new(key, purpose.encode() + b"\x00" + value.encode(), hashlib.sha256).hexdigest()


def hmac_token(key: bytes, value: str, purpose: str = "subject") -> str:
    """tok_ plus 32 hex. Stable for the same key, purpose and value. Subject tokens use purpose 'subject'."""
    return "tok_" + hmac_hex(key, purpose, value)[:32]


@runtime_checkable
class Redactor(Protocol):
    """Finds named entities in free text. PresidioRedactor implements it."""

    def find(self, text: str) -> list[tuple[int, int, str]]:
        """Return (start, end, kind) spans. kind is an entity name such as PERSON."""
        ...


@dataclass
class Session:
    """Placeholder numbering for one interaction (or one verdict or outcome): the same value keeps its number."""

    numbers: dict[tuple[str, str], int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def placeholder(self, kind: str, value: str) -> str:
        k = (kind, value.casefold())
        if k not in self.numbers:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self.numbers[k] = self.counts[kind]
        return f"<{kind}_{self.numbers[k]}>"


def normalise_pii(pii: Mapping[str, str] | Iterable[str] | None) -> list[tuple[str, str]]:
    """Host-named personal values as (value, KIND), longest first. A plain iterable means kind PERSON."""
    if not pii:
        return []
    pairs = list(pii.items()) if isinstance(pii, Mapping) else [(v, "PERSON") for v in pii]
    out = []
    for value, kind in pairs:
        value = str(value).strip()
        if len(value) >= 2:
            out.append((value, re.sub(r"[^A-Z_]", "_", str(kind).upper()) or "PERSON"))
    return sorted(out, key=lambda p: -len(p[0]))


class Pseudonymiser:
    def __init__(self, key: bytes, *, text: TextMode = "tokenize", redactor: Redactor | None = None, keep: Iterable[str] = ()) -> None:
        self.key = key
        self.text = text
        self.redactor = redactor
        self.keep = frozenset({"role", "type", *keep})

    # ------------------------------------------------------------ strings

    def _known(self, s: str, known: list[tuple[str, str]], session: Session) -> str:
        for value, kind in known:
            if value.casefold() in s.casefold():

                def repl(m: re.Match[str], kind: str = kind) -> str:
                    return session.placeholder(kind, m.group(0))

                s = re.sub(re.escape(value), repl, s, flags=re.IGNORECASE)
        return s

    def _patterns(self, s: str, session: Session) -> str:
        for kind, pat in _PATTERNS:
            need = _min_digits(kind)

            def repl(m: re.Match[str], kind: str = kind, need: int = need) -> str:
                if need and sum(c.isdigit() for c in m.group(0)) < need:
                    return m.group(0)
                return session.placeholder(kind, m.group(0))

            s = pat.sub(repl, s)
        return s

    def _ner(self, s: str, session: Session) -> str:
        assert self.redactor is not None
        spans = sorted(self.redactor.find(s), key=lambda t: t[0])
        out, pos = [], 0
        for start, end, kind in spans:
            if start < pos or end <= start:
                continue
            piece = s[start:end]
            out.append(s[pos:start])
            out.append(piece if _PLACEHOLDER.match(piece) else session.placeholder(re.sub(r"[^A-Z_]", "_", kind.upper()), piece))
            pos = end
        out.append(s[pos:])
        return "".join(out)

    def token(self, s: str) -> str:
        return f"<TEXT:{hmac_token(self.key, s, 'text')}>"

    def string(self, s: str, session: Session, known: list[tuple[str, str]], *, kept: bool = False) -> str:
        if not s:
            return s
        cleaned = self._patterns(self._known(s, known, session), session)
        if kept:
            return cleaned if _IDENT.match(cleaned) or _PLACEHOLDER.match(cleaned) else self.token(s)
        if _PLACEHOLDER.match(cleaned):
            return cleaned
        if self.text == "redact" and self.redactor is not None:
            return self._ner(cleaned, session)
        return self.token(s)

    # ------------------------------------------------------------ structures

    def value(self, v: Any, session: Session, known: list[tuple[str, str]], *, kept: bool = False) -> Any:
        if isinstance(v, str):
            return self.string(v, session, known, kept=kept)
        if isinstance(v, bool) or v is None:
            return v
        if isinstance(v, int | float):
            if not kept and abs(v) >= _BIG_NUMBER:
                return f"<NUMBER:{hmac_token(self.key, repr(v), 'number')}>"
            return v
        if isinstance(v, Mapping):
            out: dict[str, Any] = {}
            for k, item in v.items():
                ks = str(k)
                pk = ks if _FIELD.match(ks) and ks == self._patterns(self._known(ks, known, session), session) else self.token(ks)
                out[pk] = self.value(item, session, known, kept=ks in self.keep)
            return out
        if isinstance(v, list | tuple):
            return [self.value(item, session, known, kept=kept) for item in v]
        # Unknown objects are never stored as they are.
        return self.token(repr(v))

    def safe_id(self, value: str | None) -> bool:
        """True for a plain id (letters, digits, _ . : -, at most 128) that holds no pattern entity."""
        return bool(value) and bool(_ID.match(value or "")) and self._patterns(value or "", Session()) == value

    def labels(self, labels: Mapping[str, str], session: Session, known: list[tuple[str, str]]) -> dict[str, str]:
        """Slice labels from the host: kept like `keep` fields."""
        return {self.string(str(k), session, known, kept=True): self.string(str(v), session, known, kept=True) for k, v in labels.items()}
