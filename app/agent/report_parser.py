"""모델 응답을 BugSpec으로 검증하고 출처를 확인한다."""

import json
import unicodedata
from time import perf_counter

from app.agent.model_client import JsonModelClient
from app.agent.prompts import REPORT_PARSER_PROMPT
from app.agent.report_schema import (
    BugSpec,
    ExpectationSource,
)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return " ".join(text.split())


class ReportParser:
    def __init__(self, client: JsonModelClient):
        self.client = client

    async def parse(
        self,
        report: str,
        public_spec: str = "",
    ) -> dict:
        if not report.strip():
            raise ValueError("제보가 비어 있습니다.")

        schema = BugSpec.model_json_schema()

        messages = [
            {
                "role": "system",
                "content": REPORT_PARSER_PROMPT,
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "report": report,
                        "public_spec": public_spec,
                        "output_schema": schema,
                    },
                    ensure_ascii=False,
                ),
            },
        ]

        started = perf_counter()

        response = await self.client.generate(
            messages=messages,
            schema=schema,
        )

        spec = BugSpec.model_validate_json(
            response["content"]
        )

        # 출처가 명시됐다면 근거 문장이 실제 원문에 있어야 한다.
        if spec.expectation_source != ExpectationSource.UNKNOWN:
            source = (
                report
                if spec.expectation_source
                == ExpectationSource.REPORT
                else public_spec
            )

            evidence = normalize(
                spec.expectation_evidence or ""
            )

            if not evidence or evidence not in normalize(source):
                raise ValueError(
                    "기대 동작의 근거가 지정한 원문에 없습니다."
                )

        return {
            "spec": spec,
            "usage": response["usage"],
            "elapsed_seconds": round(
                perf_counter() - started, 2
            ),
        }