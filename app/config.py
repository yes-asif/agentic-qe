"""
Environment-driven settings. Secrets abstraction is deliberately thin (.env) but
isolated behind this module so swapping in Vault/AWS Secrets Manager later only
touches `Settings.load_secret()`, not call sites.
"""

from __future__ import annotations
import os
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Cloud LLM keys (LiteLLM reads these by standard env var name too) ---
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    # --- Local LLM (Ollama) ---
    ollama_base_url: str = "http://localhost:11434"

    # --- Per-role model routing defaults (overridable per test-suite config) ---
    ingestion_model: str = "claude-sonnet-4-6"
    executor_model: str = "claude-sonnet-4-6"
    healer_model: str = "ollama/qwen2.5:32b-instruct"
    triage_model: str = "claude-sonnet-4-6"

    # --- Playwright MCP server ---
    mcp_server_command: str = "npx"
    mcp_server_args: list[str] = ["-y", "@playwright/mcp@latest", "--headless"]

    # --- Cache ---
    cache_db_path: str = "./data/selector_cache.sqlite3"

    # --- Reporting ---
    reports_dir: str = "./data/reports"

    # --- Healing ---
    default_max_healing_retries: int = 3

    def load_secret(self, env_var_name: str) -> str | None:
        """Single seam for secret retrieval; swap for Vault/SM later."""
        return os.environ.get(env_var_name)


@lru_cache
def get_settings() -> Settings:
    return Settings()
