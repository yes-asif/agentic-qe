"""Ingestion/Gen Agent node: User Story + AC -> Gherkin + semantic TestSteps."""

from __future__ import annotations

import logging
import time
import uuid

from app.state import QAAgentState, TestStep
from app.llm_router import call_llm_json, resolve_model_for_role
from app.agents.prompts import INGESTION_SYSTEM_PROMPT
from app.cache import make_cache_key

logger = logging.getLogger("agentic_qe.agents.ingestion")


async def ingestion_node(state: QAAgentState) -> dict:
    model = resolve_model_for_role(state["llm_provider_config"], "ingestion")

    user_prompt = (
        f"USER STORY:\n{state['user_story']}\n\n"
        f"ACCEPTANCE CRITERIA:\n"
        + "\n".join(f"- {ac}" for ac in state["acceptance_criteria"])
    )

    result = await call_llm_json(
        model=model,
        system_prompt=INGESTION_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        temperature=0.2,
        max_tokens=4096,
    )

    base_url = state["target_config"].get("base_url", "")
    test_steps: list[TestStep] = []
    for raw_step in result.get("test_steps", []):
        gherkin_line = raw_step["gherkin_line"]
        test_steps.append(
            TestStep(
                step_id=raw_step.get("step_id") or f"step_{uuid.uuid4().hex[:8]}",
                gherkin_line=gherkin_line,
                intent_type=raw_step["intent_type"],
                semantic_instruction=raw_step["semantic_instruction"],
                extract_schema=raw_step.get("extract_schema"),
                assertion_expected=raw_step.get("assertion_expected"),
                status="pending",
                result=None,
                error=None,
                cache_key=make_cache_key(base_url, gherkin_line),
            )
        )

    logger.info("Ingestion produced %d test steps for run %s", len(test_steps), state["run_id"])

    return {
        "gherkin_feature": result.get("gherkin_feature", ""),
        "test_steps": test_steps,
        "current_step_index": 0,
        "execution_log": [{
            "ts": time.time(),
            "node": "ingestion",
            "event": "steps_generated",
            "detail": {"step_count": len(test_steps)},
        }],
        "final_status": "running",
    }
