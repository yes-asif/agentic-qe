"""
Playwright MCP Executor Node.

Design note: LangGraph node functions are plain `state -> partial_state` async
callables, but the executor needs a *live* MCP session (subprocess + protocol
handshake) that persists across the whole run rather than being recreated per
node call. We solve this with a factory (`make_executor_node`) that closes over
a already-initialized StagehandMCPClient + SelectorCache, both constructed once
per run in app/graph.py::build_graph_for_run.
"""

from __future__ import annotations

import logging
import time

from app.state import QAAgentState
from app.mcp_client import StagehandMCPClient, MCPExecutionError
from app.cache import SelectorCache

logger = logging.getLogger("agentic_qe.agents.executor")


def make_executor_node(client: StagehandMCPClient, cache: SelectorCache):
    async def executor_node(state: QAAgentState) -> dict:
        idx = state["current_step_index"]
        steps = state["test_steps"]

        if idx >= len(steps):
            # Nothing left to run - shouldn't normally be reached because the
            # conditional edge routes to END first, but guard defensively.
            return {"final_status": "passed"}

        step = dict(steps[idx])  # shallow copy - we mutate and write back below
        instruction = step["semantic_instruction"]

        # --- Cache lookup: if this exact gherkin line was healed before against
        # this same target, use the healed instruction directly and skip straight
        # past any potential failure - deterministic replay. ---
        cached = await cache.get(step["cache_key"])
        if cached:
            instruction = cached.healed_intent
            logger.info("Cache hit for step %s -> using healed intent from prior run", step["step_id"])

        log_entry = {"ts": time.time(), "node": "executor", "step_id": step["step_id"], "instruction": instruction}

        try:
            outcome = await _run_intent(client, step["intent_type"], instruction, step.get("extract_schema"), step.get("assertion_expected"))
            step["status"] = "passed"
            step["result"] = outcome
            step["error"] = None
            log_entry["event"] = "step_passed"
            log_entry["detail"] = outcome

            new_steps = list(steps)
            new_steps[idx] = step

            next_index = idx + 1
            is_last = next_index >= len(new_steps)

            return {
                "test_steps": new_steps,
                "current_step_index": next_index,
                "healing_retry_count": 0,  # reset for the next step
                "execution_log": [log_entry],
                "final_status": "passed" if is_last else "running",
            }

        except MCPExecutionError as exc:
            step["status"] = "failed"
            step["error"] = str(exc)
            log_entry["event"] = "step_failed"
            log_entry["detail"] = {"error": str(exc)}

            new_steps = list(steps)
            new_steps[idx] = step

            return {
                "test_steps": new_steps,
                "execution_log": [log_entry],
                # current_step_index intentionally NOT advanced - Healer retries in place
                "_last_error_trace": str(exc),          # transient, read by Healer/Triage
                "_last_dom_snapshot": exc.dom_snapshot,   # transient
            }

    return executor_node


async def _run_intent(client: StagehandMCPClient, intent_type: str, instruction: str, extract_schema: dict | None, assertion_expected: str | None) -> dict:
    if intent_type == "act":
        result = await client.act(instruction)
        return {"tool_called": result.tool_called, "tool_args": result.tool_args, "raw": result.raw_result}

    if intent_type == "observe":
        result = await client.observe(instruction)
        return {"candidates": result.candidates}

    if intent_type == "extract":
        data = await client.extract(instruction, extract_schema or {})
        return {"extracted": data}

    if intent_type == "assert":
        # Assertions extract the relevant state, then have the LLM judge it against
        # the expected outcome - still selector-free, still structured.
        observed = await client.extract(
            instruction=f"Extract the current state relevant to this assertion: {assertion_expected}",
            schema={"observed_state": "string", "matches_expectation": "boolean", "explanation": "string"},
        )
        if not observed.get("matches_expectation", False):
            raise MCPExecutionError(
                f"Assertion failed: expected '{assertion_expected}', observed "
                f"'{observed.get('observed_state')}' - {observed.get('explanation', '')}",
                dom_snapshot=await client._snapshot(),
                failed_instruction=instruction,
            )
        return {"assertion": observed}

    raise MCPExecutionError(f"Unknown intent_type: {intent_type}", failed_instruction=instruction)
