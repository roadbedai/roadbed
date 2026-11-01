"""PII guard for resolved rows.

Snapshot, golden and correction-test rows carry the model output, the label, the reason code and the outcome.
None of them may hold personal data or free text. Inputs are pseudonymised by the SDK; the guard still scans them
for contact-shaped strings (email, phone, US SSN, card number) as a second line. A row that trips the guard is
quarantined by the resolver: it reaches no output file.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
PHONE_RUN = re.compile(r"\+?\(?\d[\d ().-]{6,}\d")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
FREE_TEXT_CHARS = 120
FREE_TEXT_WORDS = 8
PII_KEYS = frozenset({"email", "phone", "name", "first_name", "last_name", "full_name", "address", "ssn", "dob",
                      "birth_date", "note", "comment", "text", "message", "description", "narrative"})
# A key that contains one of these names personal data (phone_number, ssn_last, claimant_email, home_address).
PII_KEY_PARTS = ("email", "phone", "ssn", "name", "address", "dob", "birth")
LONG_NUMBER = 10**8  # an integer with 9 or more digits is phone-, SSN- or card-shaped


def pii_key(k: str) -> bool:
    k = k.lower()
    return k in PII_KEYS or any(part in k for part in PII_KEY_PARTS)


def _strings(obj: Any, path: str = "") -> Iterator[tuple[str, str, str]]:
    """(path, key, value) for every string value and every number (as text), and (path, "<key>", key) for every key."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{path}/{k}", "<key>", str(k)
            yield from _strings(v, f"{path}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _strings(v, f"{path}/{i}")
    elif isinstance(obj, str):
        yield path, path.rsplit("/", 1)[-1], obj
    elif isinstance(obj, int | float) and not isinstance(obj, bool):
        yield path, "<number>", str(obj)


def contact_shaped(s: str) -> str | None:
    """Name of the first contact-shaped pattern in s, or None."""
    if EMAIL.search(s):
        return "email"
    if SSN.search(s):
        return "ssn"
    for m in PHONE_RUN.finditer(s):
        run = m.group(0)
        digits = sum(c.isdigit() for c in run)
        if 9 <= digits <= 15 and not ISO_DATE.match(run.lstrip("+(")):
            return "phone"
    for m in CARD.finditer(s):
        if 13 <= sum(c.isdigit() for c in m.group(0)) <= 19:
            return "card"
    return None


def scan_label_fields(obj: Any, where: str) -> str | None:
    """Reason a label-side value (output, verdict, outcome, reason code) is refused, or None.

    Refused: a contact-shaped string, a free-text string (more than 120 characters or 8 words), a number with 9 or
    more integer digits, a contact-shaped key, or a key that names personal data or free text. The reason never
    holds the value, and never a contact-shaped key.
    """
    for path, key, s in _strings(obj):
        if key == "<key>":
            if (kind := contact_shaped(s)) is not None:
                return f"{where}: {kind}-shaped key"
            if pii_key(s):
                return f"{where}{path}: key {s!r} names personal data or free text"
            continue
        if key == "<number>":
            if abs(float(s)) >= LONG_NUMBER:
                return f"{where}{path}: number with 9 or more digits (phone, SSN or card shaped)"
            continue
        if (kind := contact_shaped(s)) is not None:
            return f"{where}{path}: {kind}-shaped value"
        if len(s) > FREE_TEXT_CHARS or len(s.split()) > FREE_TEXT_WORDS:
            return f"{where}{path}: free text"
    return None


TOKEN = re.compile(r"^[A-Za-z0-9:_./+=-]{0,128}$")


def scan_outcome(obj: Any) -> str | None:
    """Reason a system-of-record outcome is refused, or None. Stricter than labels: every string value is a token
    (no spaces), as in audit payloads (SPEC.md, Personal data), so a name or a note cannot ride along."""
    if (why := scan_label_fields(obj, "outcome")) is not None:
        return why
    for path, key, s in _strings(obj):
        if key not in ("<key>", "<number>") and not TOKEN.match(s):
            return f"outcome{path}: not a token (free text or a name)"
    return None


def scan_input(obj: Any) -> str | None:
    """Reason a pseudonymised input is refused (contact-shaped string anywhere), or None. Free text is allowed."""
    for path, key, s in _strings(obj):
        if key not in ("<key>", "<number>") and (kind := contact_shaped(s)) is not None:
            return f"input{path}: {kind}-shaped value"
    return None
