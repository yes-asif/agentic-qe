"""
Central LangGraph state schema.

A single QAAgentState instance is threaded through every node in the graph
(Ingestion -> Executor -> Healer -> Triage). Nodes must treat this as
append-only / immutable-update: return a partial dict of the keys they
change, and LangGraph merges it into the running state.
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional, TypedDict
from operator import add


IntentType = Literal["act", "observe", "extract", "assert"]
StepStatus = Literal["pending", "passed", "failed", "healed", "skipped"]
TriageClassification = Literal[
    "functional_bug", "test_data_issue", "environment_issue", "flaky_selector_unresolved"
]
RunStatus = Literal["running", "passed", "failed", "triaged", "error"]


class MFAConfig(TypedDict, total=False):
    """Configurable MFA handling strategy - see app/agents/prompts.py for how each
    strategy is described to the LLM so it knows which tool/action to reach for."""
    strategy: Literal["totp_seed", "human_in_loop", "test_hook_bypass", "none"]
    totp_secret_env_var: Optional[str]        # e.g. "TARGET_TOTP_SECRET"
    bypass_header: Optional[str]               # e.g. "X-Test-Skip-MFA: true"
    pause_timeout_seconds: Optional[int]       # for human_in_loop


class TargetConfig(TypedDict, total=False):
    """Per-suite environment configuration, loaded from configs/*.yaml."""
    base_url: str
    auth_strategy: Literal["form_login", "sso", "api_token_seed", "none"]
    credentials_env_prefix: str                # e.g. "STAGING_" -> STAGING_USERNAME/PASSWORD
    mfa: MFAConfig
    iframe_hint_selectors: list[str]            # semantic hints, not CSS, e.g. "the payment iframe"
    shadow_dom_hint_components: list[str]       # e.g. "the <custom-datepicker> web component"
    viewport: dict                              # {"width": 1440, "height": 900}


class TestStep(TypedDict):
    """One executable unit derived from a Gherkin line."""
    step_id: str
    gherkin_line: str
    intent_type: IntentType                    # act | observe | extract | assert
    semantic_instruction: str                  # natural-language instruction, e.g.
                                                 # "click the 'Add to Cart' button for the first item"
    extract_schema: Optional[dict]              # JSON schema, only for intent_type == "extract"
    assertion_expected: Optional[str]            # only for intent_type == "assert"
    status: StepStatus
    result: Optional[dict]                       # raw MCP/LLM result payload
    error: Optional[str]
    cache_key: Optional[str]                      # hash used to look up healed intent


class HealingAttempt(TypedDict):
    attempt_number: int
    step_id: str
    failed_intent: str
    error_trace: str
    dom_snapshot_excerpt: str                    # truncated accessibility-tree snapshot
    healed_intent: Optional[str]
    healed_intent_type: Optional[IntentType]
    reasoning: str
    success: bool


class TriageResult(TypedDict):
    classification: TriageClassification
    confidence: float                             # 0.0 - 1.0
    reasoning: str
    evidence: list[str]
    recommended_action: str
    step_id: str


class QAAgentState(TypedDict):
    # --- run identity & inputs ---
    run_id: str
    user_story: str
    acceptance_criteria: list[str]
    target_config: TargetConfig
    llm_provider_config: dict                     # per-role model routing, see llm_router.py

    # --- ingestion outputs ---
    gherkin_feature: str
    test_steps: list[TestStep]

    # --- execution cursor ---
    current_step_index: int
    execution_log: Annotated[list[dict], add]      # append-only event log for the UI/report

    # --- healing loop ---
    healing_attempts: Annotated[list[HealingAttempt], add]
    healing_retry_count: int                       # resets per step
    max_healing_retries: int

    # --- triage outcome ---
    triage_result: Optional[TriageResult]

    # --- terminal status ---
    final_status: RunStatus

    # --- transient scratch fields, set by Executor on failure, read by Healer/Triage,
    # cleared once consumed. Not part of the "public" contract but declared here so
    # every node can rely on `.get()` working consistently. ---
    _last_error_trace: Optional[str]
    _last_dom_snapshot: Optional[str]
