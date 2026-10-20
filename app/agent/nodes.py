"""Node boundaries. Implement each with injected dependencies and structured outputs."""
from app.agent.state import AgentState
from app.agent.deps import AgentDeps


async def parse_report(state: AgentState, deps: AgentDeps) -> dict:
    raise NotImplementedError("TODO: report → bug specification and missing information")


async def observe(state: AgentState, deps: AgentDeps) -> dict:
    raise NotImplementedError("TODO: collect visible browser state")


async def plan(state: AgentState, deps: AgentDeps) -> dict:
    raise NotImplementedError("TODO: validate a bounded action plan")


async def act(state: AgentState, deps: AgentDeps) -> dict:
    raise NotImplementedError("TODO: execute actions and record evidence")


async def assess(state: AgentState, deps: AgentDeps) -> dict:
    raise NotImplementedError("TODO: compare observed behavior with sourced expectations")


async def compile_test(state: AgentState, deps: AgentDeps) -> dict:
    raise NotImplementedError("TODO: compile actual action history")


async def replay(state: AgentState, deps: AgentDeps) -> dict:
    raise NotImplementedError("TODO: replay in isolated environments")


async def report(state: AgentState, deps: AgentDeps) -> dict:
    raise NotImplementedError("TODO: produce evidence-backed result")
