"""
Streamlit dashboard for agentic-qe.

Streamlit is a poll/rerun model, not a native WebSocket-push framework. To get
near-real-time updates anyway: a background thread holds a persistent WebSocket
connection to the FastAPI backend and drops incoming events into
`st.session_state`; the Live Run panel is wrapped in `st.fragment(run_every=1)`
so ONLY that panel re-executes on a 1s timer, reading whatever the background
thread has buffered - no full-page rerun, no re-establishing the socket.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

import pandas as pd
import requests
import streamlit as st
import websocket  # websocket-client

BACKEND_HTTP = os.environ.get("BACKEND_HTTP_URL", "http://backend:8000")
BACKEND_WS = os.environ.get("BACKEND_WS_URL", "ws://backend:8000")

st.set_page_config(page_title="agentic-qe", layout="wide")

# --------------------------------------------------------------------------- #
# Background WebSocket listener
# --------------------------------------------------------------------------- #

def _ws_listener(run_id: str, buffer: list, stop_event: threading.Event):
    try:
        ws = websocket.create_connection(f"{BACKEND_WS}/ws/runs/{run_id}", timeout=5)
    except Exception as exc:  # noqa: BLE001
        buffer.append({"type": "error", "message": f"ws connect failed: {exc}"})
        return
    while not stop_event.is_set():
        try:
            ws.settimeout(1.0)
            raw = ws.recv()
        except Exception:
            continue
        if not raw:
            continue
        event = json.loads(raw)
        buffer.append(event)
        if event.get("type") == "run_complete":
            break
    ws.close()


def _start_live_watch(run_id: str):
    if "live_buffer" not in st.session_state:
        st.session_state.live_buffer = []
    if st.session_state.get("watched_run_id") != run_id:
        # stop any previous watcher
        old_stop = st.session_state.get("stop_event")
        if old_stop:
            old_stop.set()
        st.session_state.live_buffer = []
        stop_event = threading.Event()
        st.session_state.stop_event = stop_event
        st.session_state.watched_run_id = run_id
        thread = threading.Thread(target=_ws_listener, args=(run_id, st.session_state.live_buffer, stop_event), daemon=True)
        thread.start()


# --------------------------------------------------------------------------- #
# Panels
# --------------------------------------------------------------------------- #

def panel_new_run():
    st.header("Start a New Test Run")

    with st.form("new_run_form"):
        col1, col2 = st.columns(2)
        with col1:
            user_story = st.text_area("User Story", height=120, placeholder="As a returning customer, I want to...")
            ac_raw = st.text_area("Acceptance Criteria (one per line)", height=120,
                                   placeholder="- Given I am logged in\n- When I add an item to cart\n- Then the cart badge shows 1")
        with col2:
            base_url = st.text_input("Target base URL", placeholder="https://staging.example.com")
            auth_strategy = st.selectbox("Auth strategy", ["none", "form_login", "sso", "api_token_seed"])
            mfa_strategy = st.selectbox("MFA strategy", ["none", "totp_seed", "human_in_loop", "test_hook_bypass"])
            totp_env_var = st.text_input("TOTP secret env var (if applicable)", value="TARGET_TOTP_SECRET")

        st.subheader("LLM Routing (LiteLLM model strings)")
        c1, c2, c3, c4 = st.columns(4)
        ingestion_model = c1.text_input("Ingestion", value="claude-sonnet-4-6")
        executor_model = c2.text_input("Executor", value="claude-sonnet-4-6")
        healer_model = c3.text_input("Healer", value="ollama/qwen2.5:32b-instruct")
        triage_model = c4.text_input("Triage", value="claude-sonnet-4-6")

        max_retries = st.slider("Max healing retries per step", 1, 5, 3)
        submitted = st.form_submit_button("Run", type="primary")

    if submitted:
        payload = {
            "user_story": user_story,
            "acceptance_criteria": [l.strip("- ").strip() for l in ac_raw.splitlines() if l.strip()],
            "target_config": {
                "base_url": base_url,
                "auth_strategy": auth_strategy,
                "mfa": {"strategy": mfa_strategy, "totp_secret_env_var": totp_env_var},
            },
            "llm_provider_config": {
                "ingestion": ingestion_model, "executor": executor_model,
                "healer": healer_model, "triage": triage_model,
            },
            "max_healing_retries": max_retries,
        }
        resp = requests.post(f"{BACKEND_HTTP}/runs", json=payload, timeout=10)
        resp.raise_for_status()
        run_id = resp.json()["run_id"]
        st.session_state.active_run_id = run_id
        _start_live_watch(run_id)
        st.success(f"Run started: {run_id}")
        st.rerun()


@st.fragment(run_every=1)
def panel_live_run():
    st.header("Live Run")
    run_id = st.session_state.get("active_run_id")
    if not run_id:
        st.info("Start a run above, or pick a past run_id below to re-attach.")
        manual_id = st.text_input("Attach to run_id")
        if manual_id:
            st.session_state.active_run_id = manual_id
            _start_live_watch(manual_id)
        return

    _start_live_watch(run_id)
    buffer = st.session_state.get("live_buffer", [])

    latest_state = None
    for event in reversed(buffer):
        if event.get("type") == "state_update":
            latest_state = event["state"]
            break

    if latest_state is None:
        st.info(f"Waiting for events from {run_id}...")
        return

    status = latest_state.get("final_status", "running")
    badge = {"running": "🔵", "passed": "🟢", "failed": "🔴", "triaged": "🟠", "error": "⚫"}.get(status, "⚪")
    st.subheader(f"{badge} {run_id} — {status.upper()}")

    steps = latest_state.get("test_steps", [])
    if steps:
        step_rows = [{
            "step": s["gherkin_line"], "type": s["intent_type"],
            "status": s["status"], "instruction": s["semantic_instruction"],
            "error": s.get("error") or "",
        } for s in steps]
        st.dataframe(pd.DataFrame(step_rows), use_container_width=True, hide_index=True)

    healing_attempts = latest_state.get("healing_attempts", [])
    if healing_attempts:
        with st.expander(f"Healing attempts ({len(healing_attempts)})", expanded=True):
            for a in healing_attempts:
                icon = "✅" if a["success"] else "❌"
                st.markdown(f"{icon} **attempt #{a['attempt_number']}** on `{a['step_id']}` — {a['reasoning']}")
                if a.get("healed_intent"):
                    st.code(a["healed_intent"], language="text")

    triage = latest_state.get("triage_result")
    if triage:
        st.warning(
            f"**Triage: {triage['classification']}** (confidence {triage['confidence']:.2f})\n\n"
            f"{triage['reasoning']}\n\n**Recommended action:** {triage['recommended_action']}"
        )

    with st.expander("Raw execution log"):
        for entry in latest_state.get("execution_log", [])[-50:]:
            ts = datetime.fromtimestamp(entry["ts"]).strftime("%H:%M:%S")
            st.text(f"[{ts}] {entry.get('node')}: {entry.get('event')}")


def panel_history():
    st.header("Run History & Trends")
    try:
        runs = requests.get(f"{BACKEND_HTTP}/runs", timeout=10).json()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not reach backend: {exc}")
        return
    if not runs:
        st.info("No completed runs yet.")
        return

    df = pd.DataFrame(runs)
    col1, col2 = st.columns(2)
    with col1:
        st.metric("Pass rate", f"{(df['final_status'] == 'passed').mean() * 100:.0f}%")
        st.bar_chart(df["final_status"].value_counts())
    with col2:
        if "triage_classification" in df.columns:
            triaged = df[df["triage_classification"].notna()]
            if not triaged.empty:
                st.bar_chart(triaged["triage_classification"].value_counts())
                st.caption("Triage classification breakdown")

    st.dataframe(df, use_container_width=True, hide_index=True)

    selected = st.selectbox("Inspect a run", df["run_id"].tolist())
    if selected:
        st.session_state.active_run_id = selected
        detail = requests.get(f"{BACKEND_HTTP}/runs/{selected}", timeout=10).json()
        st.json(detail, expanded=False)


def panel_cache_browser():
    st.header("Selector / Intent Cache")
    st.caption("Healed instructions are cached per (target, gherkin line) so future runs skip the Healer entirely.")

    target_filter = st.text_input("Filter by target base URL (optional)")
    try:
        params = {"target": target_filter} if target_filter else {}
        entries = requests.get(f"{BACKEND_HTTP}/cache", params=params, timeout=10).json()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not reach backend: {exc}")
        return

    if not entries:
        st.info("Cache is empty.")
        return

    df = pd.DataFrame(entries)
    st.dataframe(df, use_container_width=True, hide_index=True)

    col1, col2 = st.columns(2)
    with col1:
        purge_key = st.text_input("cache_key to purge")
        if st.button("Purge entry") and purge_key:
            requests.delete(f"{BACKEND_HTTP}/cache/{purge_key}", timeout=10)
            st.success(f"Purged {purge_key}")
            st.rerun()
    with col2:
        if st.button("Purge ALL cache entries", type="secondary"):
            requests.delete(f"{BACKEND_HTTP}/cache", timeout=10)
            st.success("Cache cleared")
            st.rerun()


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #

st.title("🤖 agentic-qe")

tab_run, tab_live, tab_history, tab_cache = st.tabs(
    ["▶️ New Run", "📡 Live Run", "📊 History & Trends", "🗄️ Selector Cache"]
)

with tab_run:
    panel_new_run()

with tab_live:
    panel_live_run()

with tab_history:
    panel_history()

with tab_cache:
    panel_cache_browser()
