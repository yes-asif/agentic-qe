"""Triage Agent Node: classifies an unhealable failure against the original
acceptance criteria as functional bug / test data / environment / unresolved-flake."""

from __future__ import annotations

import logging
import time

from app.state import QAAgentState, TriageResult
from app.llm_router import call_llm_json, resolve_model_for_role
from app.agents.prompts import TRIAGE_SYSTEM_PROMPT

logger = logging.getLogger("agentic_qe.agents.triage")


async def triage_node(state: QAAgentState) -> dict:
    idx = state["current_step_index"]
    step = state["test_steps"][idx]
    model = resolve_model_for_role(state["llm_provider_config"], "triage")

    relevant_healing_attempts = [
        a for a in state["healing_attempts"] if a["step_id"] == step["step_id"]
    ]

    user_prompt = (
        f"USER STORY:\n{state['user_story']}\n\n"
        f"ACCEPTANCE CRITERIA:\n" + "\n".join(f"- {ac}" for ac in state["acceptance_criteria"]) + "\n\n"
        f"FAILED STEP:\n  gherkin: {step['gherkin_line']}\n"
        f"  intent: {step['semantic_instruction']}\n"
        f"  last error: {step.get('error') or state.get('_last_error_trace', '')}\n\n"
        f"HEALING ATTEMPTS MADE ({len(relevant_healing_attempts)}):\n"
        + "\n".join(
            f"  attempt #{a['attempt_number']}: healable={a['success']}, reasoning={a['reasoning']}"
            for a in relevant_healing_attempts
        )
        + "\n\n"
        f"FULL EXECUTION LOG SO FAR:\n"
        + "\n".join(f"  [{e.get('node')}] {e.get('event')}: {e.get('detail')}" for e in state["execution_log"][-20:])
    )

    diagnosis = await call_llm_json(
        model=model,
        system_prompt=TRIAGE_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        temperature=0.1,
    )

    triage_result = TriageResult(
        classification=diagnosis.get("classification", "flaky_selector_unresolved"),
        confidence=diagnosis.get("confidence", 0.0),
        reasoning=diagnosis.get("reasoning", ""),
        evidence=diagnosis.get("evidence", []),
        recommended_action=diagnosis.get("recommended_action", ""),
        step_id=step["step_id"],
    )

    logger.info("Triage classified step %s as %s (confidence=%.2f)", step["step_id"], triage_result["classification"], triage_result["confidence"])

    return {
        "triage_result": triage_result,
        "final_status": "triaged",
        "execution_log": [{
            "ts": time.time(), "node": "triage", "step_id": step["step_id"],
            "event": "triaged", "detail": dict(triage_result),
        }],
    }
