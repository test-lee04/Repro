"""모델 제공자와 제보 해석 로직을 분리한다."""
"""현재는 Ollama 모델만 지원한다."""

import os
from typing import Protocol

from ollama import AsyncClient


class JsonModelClient(Protocol):
    async def generate(
        self,
        messages: list[dict],
        schema: dict,
    ) -> dict: ...


class OllamaJsonClient:
    def __init__(self):
        self.model = os.getenv(
            "OLLAMA_MODEL", "qwen3:8b"
        )
        self.client = AsyncClient(
            host=os.getenv(
                "OLLAMA_BASE_URL",
                "http://localhost:11434",
            ),
            timeout=float(
                os.getenv(
                    "OLLAMA_TIMEOUT_SECONDS", "180"
                )
            ),
        )

    async def generate(
        self,
        messages: list[dict],
        schema: dict,
    ) -> dict:
        response = await self.client.chat(
            model=self.model,
            messages=messages,
            format=schema,
            stream=False,
            think=False,
            options={"temperature": 0},
        )

        return {
            "content": response.message.content,
            "usage": {
                "backend": "ollama",
                "model": self.model,
                "input_tokens": response.prompt_eval_count,
                "output_tokens": response.eval_count,
            },
        }


def build_model_client() -> JsonModelClient:
    backend = os.getenv("LLM_BACKEND", "ollama")

    if backend == "ollama":
        return OllamaJsonClient()

    raise ValueError(
        f"아직 지원하지 않는 모델 연결 방식입니다: {backend}"
    )