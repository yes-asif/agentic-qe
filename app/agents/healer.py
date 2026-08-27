"""
Healer Agent Node.

Triggered when the Executor's most recent attempt failed. Analyzes the failed
intent + error trace + current DOM snapshot, proposes a corrected semantic
instruction, writes it to the SelectorCache (so subsequent runs skip healing
entirely), and hands control back to the Executor to retry the SAME step index
in place. If it cannot produce a confident fix, it flags the step unhealable so
the conditional edge routes to Triage instead.
"""

from __future__ import annotations

import logging
import time

from app.state import QAAgentState, HealingAttempt
from app.llm_router import call_llm_json, resolve_model_for_role
from app.agents.prompts import HEALER_SYSTEM_PROMPT
from app.cache import SelectorCache

logger = logging.getLogger("agentic_qe.agents.healer")


def make_healer_node(cache: SelectorCache):
    async def healer_node(state: QAAgentState) -> dict:
        idx = state["current_step_index"]
        step = state["test_steps"][idx]
        model = resolve_model_for_role(state["llm_provider_config"], "healer")

        error_trace = state.get("_last_error_trace", "") or ""
        dom_snapshot = state.get("_last_dom_snapshot", "") or ""
        attempt_number = state["healing_retry_count"] + 1

        user_prompt = (
            f"FAILED SEMANTIC INSTRUCTION: {step['semantic_instruction']}\n"
            f"INTENT TYPE: {step['intent_type']}\n"
            f"GHERKIN LINE: {step['gherkin_line']}\n\n"
            f"ERROR TRACE:\n{error_trace}\n\n"
            f"CURRENT DOM ACCESSIBILITY SNAPSHOT:\n{dom_snapshot[:6000]}\n\n"
            f"This is healing attempt #{attempt_number} of {state['max_healing_retries']} for this step."
        )

        diagnosis = await call_llm_json(
            model=model,
            system_prompt=HEALER_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.15,
        )

        healable = diagnosis.get("healable", False) and diagnosis.get("confidence", 0) >= 0.5

        attempt = HealingAttempt(
            attempt_number=attempt_number,
            step_id=step["step_id"],
            failed_intent=step["semantic_instruction"],
            error_trace=error_trace[:2000],
            dom_snapshot_excerpt=dom_snapshot[:2000],
            healed_intent=diagnosis.get("healed_instruction"),
            healed_intent_type=diagnosis.get("healed_intent_type"),
            reasoning=diagnosis.get("reasoning", ""),
            success=healable,
        )

        log_entry = {
            "ts": time.time(), "node": "healer", "step_id": step["step_id"],
            "event": "healing_succeeded" if healable else "healing_failed",
            "detail": {"confidence": diagnosis.get("confidence"), "reasoning": diagnosis.get("reasoning")},
        }

        retries_exhausted = attempt_number >= state["max_healing_retries"]

        if not healable or retries_exhausted:
            # Route to Triage. Leave test_steps untouched except marking status;
            # Triage reads healing_attempts + execution_log for context.
            new_steps = list(state["test_steps"])
            updated_step = dict(new_steps[idx])
            updated_step["status"] = "failed"
            new_steps[idx] = updated_step
            return {
                "test_steps": new_steps,
                "healing_attempts": [attempt],
                "healing_retry_count": attempt_number,
                "execution_log": [log_entry],
            }

        # Healable: update the step's instruction in place, bump the cache, loop
        # back to the Executor to retry the SAME step index.
        new_steps = list(state["test_steps"])
        updated_step = dict(new_steps[idx])
        updated_step["semantic_instruction"] = attempt["healed_intent"]
        updated_step["intent_type"] = attempt["healed_intent_type"] or updated_step["intent_type"]
        updated_step["status"] = "healed"
        new_steps[idx] = updated_step

        await cache.put(
            cache_key=updated_step["cache_key"],
            target_base_url=state["target_config"].get("base_url", ""),
            gherkin_line=updated_step["gherkin_line"],
            original_intent=step["semantic_instruction"],
            original_intent_type=step["intent_type"],
            healed_intent=attempt["healed_intent"],
            healed_intent_type=attempt["healed_intent_type"] or step["intent_type"],
            healing_reasoning=attempt["reasoning"],
        )

        return {
            "test_steps": new_steps,
            "healing_attempts": [attempt],
            "healing_retry_count": attempt_number,
            "execution_log": [log_entry],
        }

    return healer_node
