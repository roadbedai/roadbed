"""Presidio named-entity redaction, the optional `[redact]` extra."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ._errors import ConfigError

# Entities that are personal data in the demo domain (insurance claims). Presidio names.
DEFAULT_ENTITIES = (
    "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "LOCATION", "IBAN_CODE", "CREDIT_CARD",
    "US_SSN", "IP_ADDRESS", "DATE_TIME", "NRP", "MEDICAL_LICENSE", "URL",
)


class PresidioRedactor:
    """Redactor backed by presidio_analyzer. Install with `pip install 'roadbed-sdk[redact]'` plus a spaCy model."""

    def __init__(self, *, language: str = "en", entities: Iterable[str] = DEFAULT_ENTITIES, score_threshold: float = 0.3,
                 engine: Any | None = None) -> None:
        if engine is None:
            try:
                from presidio_analyzer import AnalyzerEngine
            except ImportError as e:
                raise ConfigError(
                    "roadbed_sdk: text='redact' needs Presidio. Install it with: pip install 'roadbed-sdk[redact]', "
                    "then a spaCy model (python -m spacy download en_core_web_lg). Or use text='tokenize'."
                ) from e
            engine = AnalyzerEngine()
        self._engine = engine
        self._language = language
        self._entities = list(entities)
        self._threshold = score_threshold

    def find(self, text: str) -> list[tuple[int, int, str]]:
        results = self._engine.analyze(text=text, language=self._language, entities=self._entities, score_threshold=self._threshold)
        return [(r.start, r.end, r.entity_type) for r in results]
