"""Typed application settings.

All runtime configuration enters through :class:`HoursXSettings`; no other module
reads environment variables directly. Values come from the environment (prefix
``HOURSX_``) or an ``.env`` file.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

TaskBackend = Literal["inline", "arq"]


class HoursXSettings(BaseSettings):
    """Environment-driven configuration for every HoursX process."""

    model_config = SettingsConfigDict(env_prefix="HOURSX_", env_file=".env", extra="ignore")

    # --- identity / server ---
    environment: Literal["dev", "test", "production"] = "dev"
    host: str = "0.0.0.0"
    port: int = 8400
    public_url: str = "http://localhost:8400"

    # --- persistence ---
    database_url: str = "sqlite+aiosqlite:///./hoursx.sqlite3"
    redis_url: str | None = None

    # --- auth ---
    jwt_secret: str = Field(default="dev-only-insecure-secret", repr=False)
    jwt_ttl_seconds: int = 60 * 60 * 12
    allow_open_registration: bool = True

    # --- execution ---
    task_backend: TaskBackend = "inline"
    max_run_steps: int = 24
    tool_timeout_seconds: float = 120.0
    workspace_root: str = "./workspaces"

    # --- models ---
    anthropic_api_key: str | None = Field(default=None, repr=False)
    openai_api_key: str | None = Field(default=None, repr=False)
    gemini_api_key: str | None = Field(default=None, repr=False)
    openai_base_url: str = "https://api.openai.com/v1"
    local_base_url: str | None = None  # OpenAI-compatible local endpoint (Ollama, vLLM)
    model_aliases: dict[str, str] = Field(
        default_factory=lambda: {
            "fast": "anthropic/claude-haiku-4-5",
            "deep": "anthropic/claude-sonnet-5",
            "embed": "hash/hash-embed-256",
        }
    )
    model_fallbacks: dict[str, list[str]] = Field(default_factory=dict)

    # --- resilience ---
    provider_retry_attempts: int = 3
    breaker_failure_threshold: int = 5
    breaker_recovery_seconds: float = 30.0

    # --- host/kernel operations (deliberately opt-in) ---
    # Deep system access is a capability an operator grants, never a default.
    system_ops_enabled: bool = False
    # "direct" performs privileged operations in this process; "helper" delegates
    # them to hoursx-sysd so the agent itself needs no capabilities.
    system_backend: Literal["direct", "helper"] = "direct"
    sysd_socket: str = "/run/hoursx/sysd.sock"
    system_mutations_enabled: bool = False
    system_sysctl_allowlist: list[str] = Field(default_factory=list)

    # --- channels (an unset credential means the channel simply does not exist) ---
    # Webhooks arrive with no HoursX identity of their own, so inbound messages
    # need a workspace and an agent named here to belong to.
    channel_workspace_slug: str = ""
    channel_agent_handle: str = ""
    # Telegram: token from @BotFather; the secret is echoed back in a header on
    # every update, and is the only thing distinguishing Telegram from anyone
    # who guesses the URL.
    telegram_token: str | None = Field(default=None, repr=False)
    telegram_webhook_secret: str = Field(default="", repr=False)
    # WhatsApp Business Cloud: the app secret signs webhooks, the token sends.
    whatsapp_token: str | None = Field(default=None, repr=False)
    whatsapp_app_secret: str = Field(default="", repr=False)
    whatsapp_phone_number_id: str = ""
    # Gmail: an OAuth2 access token; refreshing it is the operator's concern.
    gmail_access_token: str | None = Field(default=None, repr=False)
    gmail_address: str = ""
    channel_reply_max_attempts: int = 5
    channel_dispatch_interval_seconds: float = 2.0

    # --- quotas (0 disables the limit) ---
    max_concurrent_runs_per_workspace: int = 8
    max_runs_per_hour_per_workspace: int = 240

    # --- knowledge ---
    chunk_size_chars: int = 1600
    chunk_overlap_chars: int = 200
    retrieval_top_k: int = 6
    page_size_default: int = 50
    page_size_max: int = 200

    # --- context budget ---
    context_token_budget: int = 24_000

    # --- plugins ---
    plugin_dir: str = "./plugins"
    marketplace_index_url: str | None = None
    mcp_servers: dict[str, dict[str, Any]] = Field(default_factory=dict)

    # --- observability ---
    log_level: str = "INFO"
    log_json: bool = True


@lru_cache(maxsize=1)
def get_settings() -> HoursXSettings:
    """Return the process-wide settings instance (cached)."""
    return HoursXSettings()
