"""
LangGraph StateGraph wiring: Ingestion -> Executor <-> Healer -> Triage -> END.

    START -> ingestion -> executor
    executor -> [more steps pending & last passed]      -> executor
    executor -> [all steps passed]                        -> END
    executor -> [step failed]                                -> healer
    healer   -> [produced a confident fix]                     -> executor  (retries SAME step)
    healer   -> [unhealable / retries exhausted]                  -> triage
    triage   -> END
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from langgraph.graph import StateGraph, END

from app.state import QAAgentState
from app.mcp_client import StagehandMCPClient
from app.cache import SelectorCache
from app.agents.ingestion import ingestion_node
from app.agents.executor import make_executor_node
from app.agents.healer import make_healer_node
from app.agents.triage import triage_node

logger = logging.getLogger("agentic_qe.graph")


def route_after_executor(state: QAAgentState) -> str:
    if state["final_status"] == "passed":
        return END
    idx = state["current_step_index"]
    step = state["test_steps"][idx]
    return "healer" if step["status"] == "failed" else "executor"


def route_after_healer(state: QAAgentState) -> str:
    idx = state["current_step_index"]
    step = state["test_steps"][idx]
    return "executor" if step["status"] == "healed" else "triage"


def build_graph(client: StagehandMCPClient, cache: SelectorCache):
    graph = StateGraph(QAAgentState)

    graph.add_node("ingestion", ingestion_node)
    graph.add_node("executor", make_executor_node(client, cache))
    graph.add_node("healer", make_healer_node(cache))
    graph.add_node("triage", triage_node)

    graph.set_entry_point("ingestion")
    graph.add_edge("ingestion", "executor")

    graph.add_conditional_edges("executor", route_after_executor, {
        "executor": "executor",
        "healer": "healer",
        END: END,
    })
    graph.add_conditional_edges("healer", route_after_healer, {
        "executor": "executor",
        "triage": "triage",
    })
    graph.add_edge("triage", END)

    return graph.compile()


async def run_qa_graph(initial_state: QAAgentState) -> AsyncIterator[dict]:
    """
    Opens the Playwright MCP session for the lifetime of the run, streams every
    node transition (for the WebSocket layer / Streamlit live view), and tears
    the session down cleanly on completion or error.
    """
    cache = SelectorCache()
    healer_and_default_model = initial_state["llm_provider_config"].get("executor", "claude-sonnet-4-6")

    async with StagehandMCPClient(initial_state["target_config"], healer_and_default_model) as client:
        compiled_graph = build_graph(client, cache)

        async for event in compiled_graph.astream(initial_state, stream_mode="values"):
            yield event
