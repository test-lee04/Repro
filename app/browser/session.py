"""One isolated browser context per run. Never reuse page state across runs."""
from typing import Protocol


class BrowserSessions(Protocol):
    async def open(self, run_id: str, target_id: str) -> str: ...
    async def close(self, session_id: str) -> None: ...
