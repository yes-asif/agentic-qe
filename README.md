# agentic-qe

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

An agentic, self-healing frontend test automation framework. LangGraph orchestrates a
cyclical multi-agent state machine (Ingestion → Executor → Healer → Triage) that drives
a browser through the **official Playwright MCP server**, using semantic
`act()` / `observe()` / `extract()` primitives instead of brittle selectors.

## Architecture at a glance

```
                         ┌─────────────────────┐
                         │   Ingestion Agent    │
                         │ (Story → Gherkin →   │
                         │  semantic TestSteps) │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                 ┌──────►│   Executor Node      │
                 │       │ (drives Playwright   │
                 │       │  MCP via act/observe/│
                 │       │  extract)            │
                 │       └──────────┬───────────┘
                 │            pass  │  fail
                 │      ┌───────────┴───────────┐
                 │      ▼                        ▼
                 │ next step / END        ┌─────────────┐
                 │                        │ Healer Node │
                 │                        │ (re-derive  │
                 │                        │  intent from│
                 │                        │  DOM + err) │
                 │                        └──────┬──────┘
                 │                    healed│      │exhausted
                 └────────────────────────────┘      ▼
                                              ┌───────────────┐
                                              │  Triage Node  │
                                              │ (bug / data / │
                                              │  env classify)│
                                              └───────┬───────┘
                                                       ▼
                                                      END
```

- **State**: a single `QAAgentState` TypedDict threaded through every node (see `app/state.py`).
- **Execution layer**: `app/mcp_client.py` wraps the official `@playwright/mcp` server
  (spawned as a subprocess and spoken to over MCP/stdio) and exposes Stagehand-style
  `act(instruction)`, `observe(instruction)`, `extract(instruction, schema)` methods.
  The LLM interprets the page's accessibility-tree snapshot and picks an MCP tool call —
  no XPath/CSS is ever authored by the agents.
- **Multi-LLM routing**: `app/llm_router.py` uses LiteLLM so cloud (Claude, GPT) and local
  (Ollama) models are called through one identical interface, configured per-agent-role.
- **Self-healing cache**: `app/cache.py` — SQLite table keyed on
  `(run_target, gherkin_step_hash)` storing the last-known-good semantic instruction, so
  a healed step becomes deterministic on the next run and skips the Healer entirely.
- **Frontend**: `frontend/streamlit_app.py` — a Streamlit dashboard with four panels
  (Live Run, Suite/Config Management, History & Trends, Selector Cache Browser). It
  attaches to the backend's WebSocket (`/ws/runs/{run_id}`) in a background thread and
  uses `st.fragment(run_every=...)` to render near-real-time updates.

## Repo layout

```
agentic-qe/
├── app/
│   ├── main.py              FastAPI app: REST + WebSocket run orchestration
│   ├── config.py             pydantic-settings, env-driven config
│   ├── state.py               LangGraph TypedDict schema
│   ├── graph.py                LangGraph StateGraph + conditional routing
│   ├── llm_router.py           LiteLLM multi-provider router
│   ├── mcp_client.py            Playwright-MCP-backed semantic act/observe/extract
│   ├── cache.py                  SQLite healed-selector/intent cache
│   ├── reporting.py               ReportSink interface (JSON + JUnit export)
│   └── agents/
│       ├── prompts.py             System prompts for every agent role
│       ├── ingestion.py            Story/AC → Gherkin + semantic TestSteps
│       ├── executor.py              Runs a TestStep against mcp_client
│       ├── healer.py                 Re-derives intent on failure
│       └── triage.py                  Bug / Data / Environment classification
├── frontend/
│   ├── streamlit_app.py
│   └── Dockerfile
├── configs/
│   └── example_suite.yaml           Example target/env/MFA config
├── docker-compose.yml
├── Dockerfile                        Backend image
└── requirements.txt
```

## Prerequisites

| Requirement | Needed for | Notes |
|---|---|---|
| Docker + Docker Compose v2 | Recommended path | `docker compose version` should print v2.x |
| Python 3.11+ | Local (non-Docker) run | `python3 --version` |
| Node.js 20+ and `npx` | Local (non-Docker) run | Required to spawn the `@playwright/mcp` server subprocess. The Docker image installs this for you automatically. |
| An Anthropic and/or OpenAI API key | Cloud LLM roles | Only required for whichever roles you route to a cloud model |
| [Ollama](https://ollama.com) | Local LLM roles | Only required for whichever roles you route to a local model (the Healer, by default) |

You do **not** need Playwright browsers installed yourself — the MCP server manages its
own browser binary (the Docker image pre-installs Chromium at build time; the local
path installs it the first time you run the server).

---

## Option A — Run with Docker Compose (recommended)

This starts three things: the FastAPI backend (which spawns the Playwright MCP server
subprocess internally), the Streamlit dashboard, and a shared data volume for the
SQLite cache + JSON/JUnit reports.

1. **Copy and fill in the env file:**
   ```bash
   cd agentic-qe
   cp .env.example .env
   ```
   Edit `.env`:
   ```bash
   ANTHROPIC_API_KEY=sk-ant-...        # required if any role uses a claude-* model
   OPENAI_API_KEY=sk-...               # required if any role uses a gpt-* model
   OLLAMA_BASE_URL=http://host.docker.internal:11434   # leave as-is if Ollama runs on your host machine
   TARGET_TOTP_SECRET=JBSWY3DPEHPK3PXP  # base32 TOTP seed for the app-under-test, if using MFA
   ```

2. **(If using local models) start Ollama on your host and pull a model:**
   ```bash
   ollama serve                        # usually already running as a service
   ollama pull qwen2.5:32b-instruct    # default Healer model — swap for whatever you configured
   ```
   The `extra_hosts: host.docker.internal:host-gateway` entry in `docker-compose.yml`
   lets the backend container reach Ollama running on your host machine. If Ollama runs
   in its own container instead, add it as a service in `docker-compose.yml` and point
   `OLLAMA_BASE_URL` at that service name instead.

3. **Build and start everything:**
   ```bash
   docker compose up --build
   ```
   First build takes a few minutes (installs Node, Playwright, and Chromium in the
   backend image). Subsequent starts are fast.

4. **Open the dashboard:**
   - Streamlit UI: **http://localhost:8501**
   - Backend API/docs: **http://localhost:8000/docs**
   - Health check: **http://localhost:8000/health**

5. **Stop everything:**
   ```bash
   docker compose down          # add -v to also wipe the cache/report volume
   ```

---

## Option B — Run locally without Docker

Useful for debugging a single agent node or iterating on prompts quickly.

1. **Create a virtualenv and install dependencies:**
   ```bash
   cd agentic-qe
   python3 -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Install the Playwright MCP server and a browser (one-time):**
   ```bash
   npx -y @playwright/mcp@latest --version
   npx -y playwright install chromium
   ```

3. **Set environment variables** (same keys as `.env.example` — either `export` them or
   `cp .env.example .env`; `pydantic-settings` reads `.env` automatically):
   ```bash
   cp .env.example .env
   # edit .env with your keys as in Option A step 1
   ```

4. **Start the backend:**
   ```bash
   uvicorn app.main:app --reload --port 8000
   ```

5. **In a second terminal, start the dashboard:**
   ```bash
   source .venv/bin/activate
   export BACKEND_HTTP_URL=http://localhost:8000
   export BACKEND_WS_URL=ws://localhost:8000
   streamlit run frontend/streamlit_app.py
   ```

6. Open **http://localhost:8501**.

---

## Running your first test

### Via the dashboard
1. Go to the **▶️ New Run** tab.
2. Fill in a **User Story** and one **Acceptance Criterion per line** — see
   `configs/example_suite.yaml` under `example_scenario` for a ready-made sample.
3. Set the **Target base URL** and, if relevant, an **Auth strategy** / **MFA strategy**.
4. Leave the LLM routing fields as-is to use the defaults, or point any role at a
   different LiteLLM model string (e.g. `ollama/llama3.1:8b`, `gpt-4.1`).
5. Click **Run**, then switch to the **📡 Live Run** tab to watch each LangGraph node
   fire in near-real-time — generated steps, pass/fail per step, healing attempts, and
   (if applicable) the final Triage classification.
6. Past runs show up under **📊 History & Trends**; every healed instruction is
   browsable and purgeable under **🗄️ Selector Cache**.

### Via the API directly
```bash
curl -X POST http://localhost:8000/runs \
  -H "Content-Type: application/json" \
  -d '{
        "user_story": "As a returning customer, I want to add an item to my cart and check out.",
        "acceptance_criteria": [
          "Given I am on the product page, when I click Add to Cart, then the cart badge shows 1 item",
          "Given items are in my cart, when I click Place Order, then I see an order confirmation number"
        ],
        "target_config": {
          "base_url": "https://staging.example-shop.com",
          "auth_strategy": "none",
          "mfa": {"strategy": "none"}
        },
        "llm_provider_config": {
          "ingestion": "claude-sonnet-4-6",
          "executor": "claude-sonnet-4-6",
          "healer": "ollama/qwen2.5:32b-instruct",
          "triage": "claude-sonnet-4-6"
        },
        "max_healing_retries": 3
      }'
# -> {"run_id": "run_xxxxxxxxxxxx"}

# Stream live progress (requires a WebSocket client, e.g. `websocat`):
websocat ws://localhost:8000/ws/runs/run_xxxxxxxxxxxx

# Or poll for the final report once complete:
curl http://localhost:8000/runs/run_xxxxxxxxxxxx
```

Full request/response schemas are auto-documented at **http://localhost:8000/docs**.

### In CI
`app/reporting.py::JUnitReportSink` writes a standard JUnit XML file per run to
`REPORTS_DIR` (default `./data/reports`, or `/data/reports` inside the Docker volume) —
point your CI's test-reporting step at that file. `exit_code_for_state()` in the same
module maps a run's `final_status` to a process exit code (`0` pass, `1` fail/triaged,
`2` internal error) if you're driving runs from a script rather than the API.

---

## Configuration reference

| Env var | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Used by LiteLLM for any `claude-*` model string |
| `OPENAI_API_KEY` | — | Used by LiteLLM for any `gpt-*` model string |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Used by LiteLLM for any `ollama/*` model string |
| `TARGET_TOTP_SECRET` | — | Base32 TOTP seed, read by the `totp_seed` MFA strategy (name is configurable per-run via `target_config.mfa.totp_secret_env_var`) |
| `INGESTION_MODEL` / `EXECUTOR_MODEL` / `HEALER_MODEL` / `TRIAGE_MODEL` | see `app/config.py` | Default per-role model routing; overridable per-run via `llm_provider_config` in the API/UI |
| `CACHE_DB_PATH` | `./data/selector_cache.sqlite3` | SQLite file for healed intents |
| `REPORTS_DIR` | `./data/reports` | Where JSON + JUnit reports land |
| `MCP_SERVER_COMMAND` / `MCP_SERVER_ARGS` | `npx` / `-y @playwright/mcp@latest --headless` | How the backend spawns the Playwright MCP server; drop `--headless` to watch the browser locally |

Per-suite target settings (base URL, auth strategy, MFA strategy, iframe/shadow-DOM
hints) are **not** environment variables — they're passed per-run in the
`target_config` object (UI form fields, or the `target_config` block in
`configs/example_suite.yaml` if you're scripting runs).

---

## Troubleshooting

- **`npx: command not found` / MCP server fails to spawn (local run)** — install
  Node.js 20+ and confirm `npx --version` works in the same shell you launch `uvicorn` from.
- **Backend can't reach Ollama** — from inside Docker, `localhost` refers to the
  container, not your host. Use `http://host.docker.internal:11434` (already the
  compose default) or run Ollama as its own compose service.
- **LLM call returns invalid JSON / retries then fails** — some local models ignore
  `response_format: json_object`. Try a model with better instruction-following, or
  lower `temperature` further in `app/llm_router.py::call_llm_json`.
- **Streamlit Live Run tab looks stuck** — it polls its internal buffer once per
  second (`st.fragment(run_every=1)`); if genuinely nothing arrives, check the backend
  logs — the run likely errored before publishing its first event.
- **A step keeps failing the same way across "healing" attempts** — check the
  **🗄️ Selector Cache** tab; a bad healed instruction may have been cached from an
  earlier bad run. Purge that entry (or all entries for the target) and re-run.

---

---

## License

Licensed under the [Apache License, Version 2.0](LICENSE). Copyright notices for
third-party dependencies are unaffected by this license — see each dependency's own
license (all are permissively licensed: MIT/BSD/Apache).

## Contributing

Issues and pull requests are welcome. By submitting a Contribution, you agree it is
licensed under Apache-2.0 per the terms in [LICENSE](LICENSE) §5 — no separate CLA
is required.

See inline docstrings in each module for implementation-level detail — this is a
blueprint intended to be extended, not a black box.
