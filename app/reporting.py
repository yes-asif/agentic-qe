"""
Pluggable reporting. `ReportSink` is the seam for future integrations (Jira,
Linear, Slack) - v1 ships a JSON file sink (used by the Streamlit history panel)
and a JUnit XML sink (for CI pipelines).
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Protocol

from junit_xml import TestSuite, TestCase

from app.config import get_settings
from app.state import QAAgentState


class ReportSink(Protocol):
    def write(self, state: QAAgentState) -> str: ...


class JSONReportSink:
    def write(self, state: QAAgentState) -> str:
        settings = get_settings()
        out_dir = Path(settings.reports_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{state['run_id']}.json"

        report = {
            "run_id": state["run_id"],
            "user_story": state["user_story"],
            "acceptance_criteria": state["acceptance_criteria"],
            "gherkin_feature": state["gherkin_feature"],
            "final_status": state["final_status"],
            "test_steps": state["test_steps"],
            "healing_attempts": state["healing_attempts"],
            "triage_result": state["triage_result"],
            "execution_log": state["execution_log"],
            "target_base_url": state["target_config"].get("base_url"),
            "completed_at": time.time(),
        }
        path.write_text(json.dumps(report, indent=2, default=str))
        return str(path)


class JUnitReportSink:
    def write(self, state: QAAgentState) -> str:
        settings = get_settings()
        out_dir = Path(settings.reports_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{state['run_id']}.junit.xml"

        cases = []
        for step in state["test_steps"]:
            case = TestCase(
                name=step["gherkin_line"],
                classname=state["run_id"],
                status=step["status"],
            )
            if step["status"] == "failed":
                case.add_failure_info(message=step.get("error") or "step failed", output=json.dumps(step.get("result")))
            cases.append(case)

        suite = TestSuite(name=f"qa_run_{state['run_id']}", test_cases=cases)
        with open(path, "w") as f:
            f.write(TestSuite.to_xml_string([suite]))
        return str(path)


def exit_code_for_state(state: QAAgentState) -> int:
    """For CI: 0 on pass, 1 on functional failure/triage, 2 on internal error."""
    if state["final_status"] == "passed":
        return 0
    if state["final_status"] == "error":
        return 2
    return 1
