"""
FastAPI backend.

  POST   /runs                 kick off a new test run (returns run_id immediately)
  GET    /runs                 list completed run reports
  GET    /runs/{run_id}        fetch one run's full report
  WS     /ws/runs/{run_id}     live stream of every LangGraph node transition
  GET    /cache                list healed-intent cache entries
  DELETE /cache/{cache_key}    purge one cache entry
  DELETE /cache                purge all cache entries (optionally ?target=<base_url>)
  GET    /health                liveness probe
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.config import get_settings
from app.state import QAAgentState
from app.graph import run_qa_graph
from app.cache import SelectorCache
from app.reporting import JSONReportSink, JUnitReportSink, exit_code_for_state

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agentic_qe.main")

app = FastAPI(title="agentic-qe")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# --------------------------------------------------------------------------- #
# In-memory pub/sub so the WebSocket layer can fan out events from the
# background task actually driving the LangGraph run. Swap for Redis pub/sub
# if you move to the multi-worker deployment variant.
# --------------------------------------------------------------------------- #

class RunBroker:
    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(run_id, []).append(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(run_id, [])
        if q in subs:
            subs.remove(q)

    async def publish(self, run_id: str, event: dict) -> None:
        for q in self._subscribers.get(run_id, []):
            await q.put(event)


broker = RunBroker()
_run_final_states: dict[str, QAAgentState] = {}  # in-memory cache of latest state per run_id


# --------------------------------------------------------------------------- #
# Request/response models
# --------------------------------------------------------------------------- #

class StartRunRequest(BaseModel):
    user_story: str
    acceptance_criteria: list[str]
    target_config: dict           # see app/state.py::TargetConfig
    llm_provider_config: Optional[dict] = None   # per-role model overrides
    max_healing_retries: Optional[int] = None


class StartRunResponse(BaseModel):
    run_id: str


# --------------------------------------------------------------------------- #
# Run lifecycle
# --------------------------------------------------------------------------- #

async def _execute_run(run_id: str, req: StartRunRequest) -> None:
    settings = get_settings()
    initial_state: QAAgentState = {
        "run_id": run_id,
        "user_story": req.user_story,
        "acceptance_criteria": req.acceptance_criteria,
        "target_config": req.target_config,
        "llm_provider_config": req.llm_provider_config or {},
        "gherkin_feature": "",
        "test_steps": [],
        "current_step_index": 0,
        "execution_log": [],
        "healing_attempts": [],
        "healing_retry_count": 0,
        "max_healing_retries": req.max_healing_retries or settings.default_max_healing_retries,
        "triage_result": None,
        "final_status": "running",
        "_last_error_trace": None,
        "_last_dom_snapshot": None,
    }

    latest_state = initial_state
    try:
        async for state_update in run_qa_graph(initial_state):
            latest_state = state_update
            await broker.publish(run_id, {"type": "state_update", "state": _serialize_for_ws(state_update)})
    except Exception as exc:  # noqa: BLE001
        logger.exception("Run %s crashed", run_id)
        latest_state = {**latest_state, "final_status": "error"}
        await broker.publish(run_id, {"type": "error", "message": str(exc)})
    finally:
        _run_final_states[run_id] = latest_state
        JSONReportSink().write(latest_state)
        JUnitReportSink().write(latest_state)
        await broker.publish(run_id, {"type": "run_complete", "final_status": latest_state["final_status"]})


def _serialize_for_ws(state: QAAgentState) -> dict:
    """Trim transient/huge fields before pushing over the wire."""
    slim = dict(state)
    slim.pop("_last_dom_snapshot", None)
    return slim


@app.post("/runs", response_model=StartRunResponse)
async def start_run(req: StartRunRequest):
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    asyncio.create_task(_execute_run(run_id, req))
    return StartRunResponse(run_id=run_id)


@app.get("/runs")
async def list_runs():
    settings = get_settings()
    reports_dir = Path(settings.reports_dir)
    if not reports_dir.exists():
        return []
    summaries = []
    for f in sorted(reports_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        data = json.loads(f.read_text())
        summaries.append({
            "run_id": data["run_id"],
            "final_status": data["final_status"],
            "target_base_url": data.get("target_base_url"),
            "completed_at": data.get("completed_at"),
            "step_count": len(data.get("test_steps", [])),
            "triage_classification": (data.get("triage_result") or {}).get("classification"),
        })
    return summaries


@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    if run_id in _run_final_states:
        return _serialize_for_ws(_run_final_states[run_id])
    settings = get_settings()
    path = Path(settings.reports_dir) / f"{run_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="run not found")
    return json.loads(path.read_text())


@app.websocket("/ws/runs/{run_id}")
async def ws_run(websocket: WebSocket, run_id: str):
    await websocket.accept()
    queue = broker.subscribe(run_id)
    try:
        # Replay last known state immediately so a client connecting mid-run
        # (or refreshing the dashboard) doesn't see a blank panel.
        if run_id in _run_final_states:
            await websocket.send_json({"type": "state_update", "state": _serialize_for_ws(_run_final_states[run_id])})
        while True:
            event = await queue.get()
            await websocket.send_json(event)
            if event.get("type") == "run_complete":
                break
    except WebSocketDisconnect:
        pass
    finally:
        broker.unsubscribe(run_id, queue)


# --------------------------------------------------------------------------- #
# Cache browser endpoints
# --------------------------------------------------------------------------- #

@app.get("/cache")
async def list_cache(target: Optional[str] = None):
    cache = SelectorCache()
    return await cache.list_all(target_base_url=target)


@app.delete("/cache/{cache_key}")
async def purge_cache_entry(cache_key: str):
    cache = SelectorCache()
    await cache.purge(cache_key)
    return {"purged": cache_key}


@app.delete("/cache")
async def purge_cache(target: Optional[str] = None):
    cache = SelectorCache()
    count = await cache.purge_all(target_base_url=target)
    return {"purged_count": count}


@app.get("/health")
async def health():
    return {"status": "ok"}
