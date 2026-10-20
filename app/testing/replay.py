from typing import Protocol


class ReplayRunner(Protocol):
    async def run(self, artifact: dict, target_id: str) -> dict: ...
