"""Initial state contract; split nested dictionaries into schemas during implementation."""
from typing import TypedDict


class AgentState(TypedDict):
    run_id: str
    report: str
    bug_spec: dict
    observations: list[dict]
    action_history: list[dict]
    assertions: list[dict]
    reproduction_status: str
    test_status: str
    replan_count: int
    tool_calls: int
    model_calls: int
    errors: list[dict]
