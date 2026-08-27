"""
Semantic execution layer built ON TOP OF the official Playwright MCP server
(`@playwright/mcp`, spawned as a subprocess and spoken to over stdio via the MCP
protocol). This module is the Stagehand-equivalent: it exposes

    await client.act(instruction: str) -> ActResult
    await client.observe(instruction: str) -> ObserveResult
    await client.extract(instruction: str, schema: dict) -> dict

No node in this codebase ever emits an XPath or CSS selector. Instead:

  1. We call the MCP server's `browser_snapshot` tool, which returns an
     accessibility-tree snapshot of the page. Playwright resolves this snapshot
     across iframes and open shadow roots automatically, so agents "see" a
     flattened semantic tree without needing to manage frame/shadow context
     themselves. Each interactive node in the snapshot carries a stable `ref`.
  2. We hand that snapshot + the natural-language instruction to an LLM, which
     returns a structured decision: which MCP tool to call (browser_click,
     browser_type, browser_drag, browser_select_option, browser_press_key, ...)
     and which `ref` to target.
  3. We execute that tool call against the live MCP server.

This indirection is exactly what makes the framework selector-proof: when the DOM
changes, the `ref` values change, but the *snapshot* is re-taken on every act()
call, so the LLM re-resolves the target from the current tree every time.
"""

from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Optional

import pyotp
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.config import get_settings
from app.llm_router import call_llm_json
from app.agents.prompts import MCP_ACTION_PLANNER_PROMPT, MCP_EXTRACTOR_PROMPT

logger = logging.getLogger("agentic_qe.mcp_client")


@dataclass
class ActResult:
    success: bool
    tool_called: str
    tool_args: dict
    raw_result: Any
    error: Optional[str] = None


@dataclass
class ObserveResult:
    candidates: list[dict] = field(default_factory=list)  # [{ref, role, name, suggested_action}]
    snapshot_excerpt: str = ""


class MCPExecutionError(RuntimeError):
    def __init__(self, message: str, dom_snapshot: str = "", failed_instruction: str = ""):
        super().__init__(message)
        self.dom_snapshot = dom_snapshot
        self.failed_instruction = failed_instruction


class StagehandMCPClient:
    """
    One instance per test run. Owns the subprocess/session lifecycle for the
    Playwright MCP server and exposes the semantic API used by the Executor
    and Healer agent nodes.
    """

    def __init__(self, target_config: dict, llm_model: str):
        self.target_config = target_config
        self.llm_model = llm_model
        self._session: Optional[ClientSession] = None
        self._stack: Optional[AsyncExitStack] = None
        self._available_tools: list[dict] = []

    async def __aenter__(self) -> "StagehandMCPClient":
        settings = get_settings()
        server_params = StdioServerParameters(
            command=settings.mcp_server_command,
            args=settings.mcp_server_args,
        )
        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(server_params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

        tools_response = await self._session.list_tools()
        self._available_tools = [
            {"name": t.name, "description": t.description, "input_schema": t.inputSchema}
            for t in tools_response.tools
        ]

        base_url = self.target_config.get("base_url")
        if base_url:
            await self._session.call_tool("browser_navigate", {"url": base_url})

        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._stack:
            await self._stack.aclose()

    # ------------------------------------------------------------------ #
    # Snapshot primitive
    # ------------------------------------------------------------------ #

    async def _snapshot(self) -> str:
        """Accessibility-tree snapshot of the current page (post iframe/shadow-DOM
        flattening, handled natively by Playwright)."""
        result = await self._session.call_tool("browser_snapshot", {})
        return _extract_text(result)

    # ------------------------------------------------------------------ #
    # act()
    # ------------------------------------------------------------------ #

    async def act(self, instruction: str) -> ActResult:
        """Executes a single natural-language intent against the live page."""
        instruction = self._maybe_resolve_mfa(instruction)

        snapshot = await self._snapshot()
        plan = await call_llm_json(
            model=self.llm_model,
            system_prompt=MCP_ACTION_PLANNER_PROMPT.format(
                available_tools=json.dumps(self._available_tools, indent=2),
                complex_ui_hints=self._complex_ui_hint_block(),
            ),
            user_prompt=(
                f"INSTRUCTION: {instruction}\n\n"
                f"CURRENT PAGE ACCESSIBILITY SNAPSHOT:\n{snapshot}"
            ),
        )

        tool_name = plan.get("tool")
        tool_args = plan.get("args", {})
        if not tool_name:
            raise MCPExecutionError(
                f"Planner could not resolve an MCP tool for instruction: {instruction!r} "
                f"(reasoning: {plan.get('reasoning', 'none given')})",
                dom_snapshot=snapshot,
                failed_instruction=instruction,
            )

        try:
            raw_result = await self._session.call_tool(tool_name, tool_args)
        except Exception as exc:  # noqa: BLE001
            raise MCPExecutionError(
                f"MCP tool '{tool_name}' failed for instruction {instruction!r}: {exc}",
                dom_snapshot=snapshot,
                failed_instruction=instruction,
            ) from exc

        if getattr(raw_result, "isError", False):
            raise MCPExecutionError(
                f"MCP tool '{tool_name}' returned an error for instruction {instruction!r}: "
                f"{_extract_text(raw_result)}",
                dom_snapshot=snapshot,
                failed_instruction=instruction,
            )

        return ActResult(success=True, tool_called=tool_name, tool_args=tool_args, raw_result=_extract_text(raw_result))

    # ------------------------------------------------------------------ #
    # observe()  - candidate discovery WITHOUT executing (used by Healer to
    # explore multiple hypotheses cheaply before committing to one act()).
    # ------------------------------------------------------------------ #

    async def observe(self, instruction: str) -> ObserveResult:
        snapshot = await self._snapshot()
        plan = await call_llm_json(
            model=self.llm_model,
            system_prompt=MCP_ACTION_PLANNER_PROMPT.format(
                available_tools=json.dumps(self._available_tools, indent=2),
                complex_ui_hints=self._complex_ui_hint_block(),
            ) + "\n\nDo NOT execute anything. Return up to 3 ranked candidate actions instead of one.",
            user_prompt=(
                f"INSTRUCTION: {instruction}\n\n"
                f"CURRENT PAGE ACCESSIBILITY SNAPSHOT:\n{snapshot}\n\n"
                'Respond as {"candidates": [{"tool": ..., "args": ..., "confidence": 0-1, "rationale": ...}, ...]}'
            ),
        )
        return ObserveResult(candidates=plan.get("candidates", []), snapshot_excerpt=snapshot[:2000])

    # ------------------------------------------------------------------ #
    # extract()
    # ------------------------------------------------------------------ #

    async def extract(self, instruction: str, schema: dict) -> dict:
        snapshot = await self._snapshot()
        result = await call_llm_json(
            model=self.llm_model,
            system_prompt=MCP_EXTRACTOR_PROMPT.format(schema=json.dumps(schema, indent=2)),
            user_prompt=(
                f"EXTRACTION INSTRUCTION: {instruction}\n\n"
                f"CURRENT PAGE ACCESSIBILITY SNAPSHOT:\n{snapshot}"
            ),
        )
        return result

    # ------------------------------------------------------------------ #
    # Complex UI handling helpers
    # ------------------------------------------------------------------ #

    def _complex_ui_hint_block(self) -> str:
        cfg = self.target_config
        lines = []
        if cfg.get("iframe_hint_selectors"):
            lines.append(
                "IFRAME CONTEXT: The following semantic regions are known iframes; the "
                "accessibility snapshot already flattens their contents, but disambiguate "
                f"between them by name/role when both look similar: {cfg['iframe_hint_selectors']}"
            )
        if cfg.get("shadow_dom_hint_components"):
            lines.append(
                "SHADOW DOM: These custom elements render inside open shadow roots and are "
                f"already flattened into the snapshot below: {cfg['shadow_dom_hint_components']}"
            )
        mfa = cfg.get("mfa", {})
        if mfa.get("strategy") and mfa["strategy"] != "none":
            lines.append(
                f"MFA STRATEGY IN EFFECT: '{mfa['strategy']}'. If the instruction concerns "
                "entering an OTP/2FA code, assume the code has ALREADY been substituted into "
                "the instruction text by the executor - just type it into the correct field."
            )
        lines.append(
            "DRAG AND DROP: use the 'browser_drag' tool with source and target refs from the "
            "snapshot; never attempt drag-and-drop via sequential mouse_down/mouse_move/mouse_up "
            "unless browser_drag is unavailable."
        )
        return "\n".join(lines) if lines else "None."

    def _maybe_resolve_mfa(self, instruction: str) -> str:
        """
        If this instruction is about entering an MFA/OTP code and a TOTP-seed
        strategy is configured, generate the current code and splice it into the
        instruction text so act()'s planner just has to type a literal value.
        Other strategies (human_in_loop, test_hook_bypass) are handled upstream
        by the Executor node, not here - see app/agents/executor.py.
        """
        mfa = self.target_config.get("mfa", {})
        if mfa.get("strategy") != "totp_seed":
            return instruction
        is_mfa_step = any(kw in instruction.lower() for kw in ("mfa", "otp", "2fa", "authentication code", "verification code"))
        if not is_mfa_step:
            return instruction

        settings = get_settings()
        secret = settings.load_secret(mfa.get("totp_secret_env_var", ""))
        if not secret:
            logger.warning("TOTP strategy configured but no secret found for env var %s", mfa.get("totp_secret_env_var"))
            return instruction

        code = pyotp.TOTP(secret).now()
        return f"{instruction} (the current one-time code to enter is: {code})"


def _extract_text(mcp_result: Any) -> str:
    """MCP tool results return a list of content blocks; flatten text blocks."""
    content = getattr(mcp_result, "content", None)
    if not content:
        return str(mcp_result)
    parts = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts) if parts else str(mcp_result)
