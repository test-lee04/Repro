from typing import Protocol


class Planner(Protocol):
    async def parse(self, report: str, public_spec: dict) -> dict: ...
    async def next_actions(self, spec: dict, observation: dict,
                           history: list[dict], remaining_budget: dict) -> list[dict]: ...
