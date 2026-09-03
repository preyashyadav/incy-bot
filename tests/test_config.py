"""Configuration contract tests.

These cover the failure mode that bit the previous version: credentials resolved at import
time, so a missing env var surfaced as a 500 from a Slack handler rather than at startup.
"""

from __future__ import annotations

import pytest

from incident_copilot.config import Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "slack_mode": "socket",
        "slack_bot_token": "xoxb-test",
        "slack_app_token": "xapp-test",
        "slack_signing_secret": "shhh",
    }
    base.update(overrides)
    # _env_file=None keeps a developer's real .env from leaking into the test.
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def test_defaults_are_usable_without_any_credentials() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.environment == "local"
    assert settings.anthropic_model == "claude-opus-5"
    assert "postgresql+psycopg" in str(settings.database_url)


def test_socket_mode_requires_app_token() -> None:
    with pytest.raises(RuntimeError, match="SLACK_APP_TOKEN"):
        _settings(slack_app_token=None).require_slack_credentials()


def test_http_mode_requires_signing_secret() -> None:
    with pytest.raises(RuntimeError, match="SLACK_SIGNING_SECRET"):
        _settings(slack_mode="http", slack_signing_secret=None).require_slack_credentials()


def test_http_mode_does_not_require_app_token() -> None:
    _settings(slack_mode="http", slack_app_token=None).require_slack_credentials()


def test_bot_token_is_always_required() -> None:
    with pytest.raises(RuntimeError, match="SLACK_BOT_TOKEN"):
        _settings(slack_bot_token=None).require_slack_credentials()


def test_secrets_do_not_leak_in_repr() -> None:
    """A Settings object ends up in logs and tracebacks; tokens must not ride along."""
    assert "xoxb-test" not in repr(_settings())
