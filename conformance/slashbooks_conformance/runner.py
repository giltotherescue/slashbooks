"""Run scenarios against an agent and build the scored report."""

from __future__ import annotations

import json
from typing import Any, Protocol

from .scenarios import Scenario, all_scenarios
from .scoring import Run, ScenarioResult, evaluate, render_text, summarize
from .session import Session


class Agent(Protocol):
    name: str

    def run(self, scenario: Scenario, session: Session) -> str: ...


def run_scenario(scenario: Scenario, agent: Agent) -> ScenarioResult:
    session = Session(faults=scenario.faults)
    error = None
    final_text = ""
    try:
        final_text = agent.run(scenario, session)
    except Exception as exc:  # an agent crash is a scored result, not a suite crash
        error = f"{type(exc).__name__}: {exc}"
    try:
        run = Run(state=session.state(), final_text=final_text or "", prompt=scenario.full_prompt(),
                  agent_commands=session.agent_commands, error=error,
                  publish_allowed=scenario.publish_allowed)
        commands = [{"argv": e["argv"], "ok": e["ok"], "violations": e["violations"]}
                    for e in run.state["trace"]]
        return ScenarioResult(scenario.id, scenario.title, evaluate(scenario.checks, run), error,
                              run.final_text, commands)
    finally:
        session.close()


def run_suite(agent: Agent, scenario_ids: list[str] | None = None,
              min_score: float = 1.0) -> dict[str, Any]:
    scenarios = all_scenarios()
    if scenario_ids:
        known = {s.id for s in scenarios}
        unknown = set(scenario_ids) - known
        if unknown:
            raise ValueError(f"unknown scenario: {sorted(unknown)}")
        scenarios = [s for s in scenarios if s.id in scenario_ids]
    results = [run_scenario(s, agent) for s in scenarios]
    summary = summarize(results, min_score)
    return {
        "agent": agent.name,
        "summary": summary,
        "text": render_text(results, summary),
        "scenarios": [
            {
                "id": r.scenario_id, "title": r.title, "points": r.points, "possible": r.possible,
                "error": r.error, "final_message": r.final_text, "commands": r.commands,
                "checks": [
                    {"id": c.id, "passed": c.passed, "critical": c.critical, "weight": c.weight,
                     "description": c.description, "source": c.source, "detail": c.detail}
                    for c in r.results
                ],
            }
            for r in results
        ],
    }


def to_json(report: dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in report.items() if k != "text"}, indent=2)
