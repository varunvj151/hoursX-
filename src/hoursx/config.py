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
