"""Typed application configuration.

One `Settings` object, loaded once, injected everywhere. Nothing in this codebase reads
`os.getenv` directly — that is how the previous version ended up with env vars resolved at
import time in three different modules with three different defaults.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

SlackMode = Literal["socket", "http"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- core -------------------------------------------------------------
    environment: Literal["local", "ci", "production"] = "local"
    log_level: str = "INFO"

    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+psycopg://copilot:copilot@localhost:5432/copilot"),
        description="SQLAlchemy URL. Use the psycopg (v3) driver.",
    )

    # ---- slack ------------------------------------------------------------
    slack_mode: SlackMode = Field(
        default="socket",
        description=(
            "'socket' opens a WebSocket to Slack — no public URL, best for local dev. "
            "'http' mounts the Bolt ASGI adapter on FastAPI — stateless and horizontally "
            "scalable, the production path."
        ),
    )
    slack_bot_token: SecretStr | None = None
    slack_signing_secret: SecretStr | None = None
    slack_app_token: SecretStr | None = Field(
        default=None,
        description="xapp-… token. Required only when slack_mode='socket'.",
    )

    # ---- anthropic --------------------------------------------------------
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        description="Optional: the SDK also resolves an `ant auth login` profile.",
    )
    anthropic_model: str = "claude-opus-5"
    agent_investigate_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    agent_max_tool_iterations: int = 12

    control_plane_backend: Literal["postgres", "memory"] = Field(
        default="postgres",
        description="'memory' is for tests and offline demos; it does not survive a restart "
        "and is not shared between processes.",
    )

    # ---- worker -----------------------------------------------------------
    worker_poll_interval_seconds: float = 1.0
    worker_visibility_timeout_seconds: int = 300
    job_max_attempts: int = 3

    # ---- approvals --------------------------------------------------------
    approval_token_ttl_seconds: int = 1800

    def require_slack_credentials(self) -> None:
        """Validate the credentials the selected transport needs.

        Called when the Bolt app is built, not at config load — migrations, the worker, and the
        test suite all need `Settings` without any Slack credentials present.

        Each transport needs a different one: Socket Mode authenticates the outbound WebSocket
        with an app-level token, while the HTTP adapter verifies inbound request signatures.
        Missing either surfaces as a confusing error deep inside Bolt, so check it here.
        """
        missing: list[str] = []
        if self.slack_bot_token is None:
            missing.append("SLACK_BOT_TOKEN")
        if self.slack_mode == "socket" and self.slack_app_token is None:
            missing.append("SLACK_APP_TOKEN (xapp-…, required for slack_mode='socket')")
        if self.slack_mode == "http" and self.slack_signing_secret is None:
            missing.append("SLACK_SIGNING_SECRET (required for slack_mode='http')")
        if missing:
            raise RuntimeError("Missing Slack configuration: " + ", ".join(missing))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
