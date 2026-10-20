from typing import Protocol


class TestCompiler(Protocol):
    def compile(self, actions: list[dict], assertions: list[dict]) -> dict:
        """Compile validated actions with fixed templates, not arbitrary model code."""
        ...
