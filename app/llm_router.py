"""
Every agent role (ingestion / executor-brain / healer / triage) calls the LLM
through this one function. Model strings follow LiteLLM convention:

    "claude-sonnet-4-6"                cloud, Anthropic
    "gpt-4.1"                          cloud, OpenAI
    "ollama/qwen2.5:32b-instruct"      local, via Ollama

Because LiteLLM normalizes the request/response shape across providers, node code
never branches on "is this cloud or local" - it just reads `llm_provider_config`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import litellm
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings

logger = logging.getLogger("agentic_qe.llm_router")

litellm.drop_params = True  # silently ignore provider-unsupported kwargs (e.g. response_format on some local models)


class LLMCallError(RuntimeError):
    pass


def _resolve_api_base(model: str) -> Optional[str]:
    settings = get_settings()
    if model.startswith("ollama/"):
        return settings.ollama_base_url
    return None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
async def call_llm_json(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.1,
    max_tokens: int = 2048,
) -> dict[str, Any]:
    """
    Calls the model and enforces a strict-JSON-only response. Every agent node in
    this framework uses this function so inter-agent communication is always
    structured JSON, never free text.
    """
    messages = [
        {"role": "system", "content": system_prompt + "\n\nRespond with ONLY valid JSON. No prose, no markdown fences."},
        {"role": "user", "content": user_prompt},
    ]

    kwargs: dict[str, Any] = dict(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    api_base = _resolve_api_base(model)
    if api_base:
        kwargs["api_base"] = api_base

    # Ask for JSON mode where the provider supports it; harmless no-op otherwise
    # because litellm.drop_params=True strips unsupported kwargs per-provider.
    kwargs["response_format"] = {"type": "json_object"}

    try:
        response = await litellm.acompletion(**kwargs)
    except Exception as exc:  # noqa: BLE001 - surface as our own error type
        logger.exception("LLM call failed for model=%s", model)
        raise LLMCallError(f"LLM call failed for model={model}: {exc}") from exc

    raw = response.choices[0].message.content or ""
    raw = _strip_markdown_fence(raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Model %s returned non-JSON: %s", model, raw[:500])
        raise LLMCallError(f"Model {model} returned invalid JSON: {exc}") from exc


def _strip_markdown_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    return text.strip()


def resolve_model_for_role(llm_provider_config: dict, role: str) -> str:
    """
    llm_provider_config example (set per-suite, defaults come from Settings):

        {
            "ingestion": "claude-sonnet-4-6",
            "executor":  "claude-sonnet-4-6",
            "healer":    "ollama/qwen2.5:32b-instruct",
            "triage":    "claude-sonnet-4-6",
        }

    Falling back to Settings ensures a partially-specified config still works.
    """
    settings = get_settings()
    defaults = {
        "ingestion": settings.ingestion_model,
        "executor": settings.executor_model,
        "healer": settings.healer_model,
        "triage": settings.triage_model,
    }
    return llm_provider_config.get(role, defaults[role])
