"""Errors. Only init() raises them: capture, verdict, outcome and flush never raise into the host app."""

from __future__ import annotations


class ConfigError(ValueError):
    """init() was called with a configuration the SDK refuses."""


class ProfileRequiredError(ConfigError):
    """Ingest without a pseudonymisation profile id. The server would answer 422; the SDK refuses first."""

    def __init__(self) -> None:
        super().__init__(
            "roadbed_sdk: profile_id is required. Ingest without a pseudonymisation profile is refused "
            "(the control plane answers 422). Pass init(..., profile_id='<profile>@v<n>'), e.g. 'insurer-default@v1'."
        )
