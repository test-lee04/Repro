"""제보를 분석하고 실제 원문 대조로 기대 동작의 출처를 확인한다."""

import json
import unicodedata
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from app.agent.model_client import JsonModelClient
from app.agent.prompts import REPORT_PARSER_PROMPT
from app.agent.report_schema import (
    BugSpec,
    ClarificationAnswer,
    ExpectationSource,
)


def normalize(text: str) -> str:
    """원문 비교를 위해 문자와 공백을 정리한다.

    Args:
        text: 비교할 문자열.

    Returns:
        유니코드와 공백을 정규화한 문자열.
    """
    text = unicodedata.normalize("NFKC", text)
    return " ".join(text.split())


class ReportParser:
    """제보 해석, 원문 근거 확인, 제한된 응답 수정을 담당한다."""

    def __init__(
        self,
        client: JsonModelClient,
        max_attempts: int = 2,
    ):
        """해석기를 준비한다.

        Args:
            client: 모델 연결 객체.
            max_attempts: 최초 호출을 포함한 최대 시도 횟수.

        Raises:
            ValueError: 시도 횟수가 양수가 아닌 경우.
        """
        if max_attempts < 1:
            raise ValueError("최대 시도 횟수는 양수여야 합니다.")

        self.client = client
        self.max_attempts = max_attempts

    async def parse(
        self,
        report: str,
        public_spec: str = "",
        answers: list[ClarificationAnswer] | None = None,
    ) -> dict:
        """제보와 누적 답변을 분석한다.

        Args:
            report: 사용자의 원래 제보.
            public_spec: 제공된 서비스 요구사항.
            answers: 실제 확인 질문과 사용자 답변 기록.

        Returns:
            검증된 해석, 사용량, 시간, 답변 기록,
            출처 보정 내역과 진단 파일 위치.

        Raises:
            ValueError: 입력이 비었거나 응답 검증에 모두 실패한 경우.
            OSError: 진단 파일 저장에 실패한 경우.

        Notes:
            원문 대조는 인용 출처를 확인하는 기능이다.
            인용이 기대 동작을 논리적으로 뒷받침하는지는
            별도 의미 검증이 필요하다.
        """
        if not report.strip():
            raise ValueError("제보가 비어 있습니다.")

        answer_history = list(answers or [])
        schema = BugSpec.model_json_schema()

        payload = {
            "report": report,
            "public_spec": public_spec,
            "answers": [
                item.model_dump()
                for item in answer_history
            ],
            "output_schema": schema,
        }

        messages = [
            {
                "role": "system",
                "content": REPORT_PARSER_PROMPT,
            },
            {
                "role": "user",
                "content": json.dumps(
                    payload,
                    ensure_ascii=False,
                ),
            },
        ]

        diagnostic_dir = Path("artifacts") / "report-parser"
        diagnostic_dir.mkdir(parents=True, exist_ok=True)

        diagnostic_path = (
            diagnostic_dir / f"{uuid4().hex}.json"
        )

        attempts = []
        started = perf_counter()

        for attempt_number in range(1, self.max_attempts + 1):
            response = await self.client.generate(
                messages=messages,
                schema=schema,
            )

            attempt = {
                "attempt": attempt_number,
                "content": response["content"],
                "usage": response["usage"],
                "validation_error": None,
                "source_correction": None,
                "evidence_matches": [],
            }

            try:
                spec = BugSpec.model_validate_json(
                    response["content"]
                )

                spec, correction, matches = self._resolve_evidence(
                    spec=spec,
                    report=report,
                    public_spec=public_spec,
                    answers=answer_history,
                )

            except ValueError as error:
                attempt["validation_error"] = str(error)
                attempts.append(attempt)

                self._save_diagnostics(
                    diagnostic_path,
                    payload,
                    attempts,
                )

                print(
                    f"해석 응답 검증 실패 "
                    f"({attempt_number}/{self.max_attempts}): "
                    f"{error}",
                    flush=True,
                )

                if attempt_number >= self.max_attempts:
                    raise ValueError(
                        "모델 응답이 형식 또는 근거 검증을 "
                        f"통과하지 못했습니다. 기록: {diagnostic_path}"
                    ) from error

                messages.extend([
                    {
                        "role": "assistant",
                        "content": response["content"],
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "validation_error": str(error),
                                "instruction": (
                                    "원래 입력 자료만 사용해 "
                                    "검증 오류를 수정하세요. "
                                    "근거는 한 원문에서 그대로 "
                                    "인용해야 합니다. "
                                    "질문 문구는 근거가 아닙니다. "
                                    "근거가 없으면 unknown과 "
                                    "확인 질문을 반환하세요. "
                                    "JSON 객체 전체를 다시 반환하세요."
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    },
                ])

                continue

            attempt["source_correction"] = correction
            attempt["evidence_matches"] = matches
            attempt["resolved_spec"] = spec.model_dump(
                mode="json"
            )
            attempts.append(attempt)

            self._save_diagnostics(
                diagnostic_path,
                payload,
                attempts,
            )

            if correction:
                print(
                    "원문 대조로 기대 동작의 출처를 보정했습니다: "
                    f"{correction['from']} → {correction['to']}",
                    flush=True,
                )

            return {
                "spec": spec,
                "usage": self._summarize_usage(attempts),
                "elapsed_seconds": round(
                    perf_counter() - started, 2
                ),
                "answers": payload["answers"],
                "attempt_count": len(attempts),
                "source_correction": correction,
                "evidence_matches": matches,
                "diagnostic_path": str(diagnostic_path),
            }

        raise RuntimeError("해석 결과를 반환하지 못했습니다.")

    @staticmethod
    def _resolve_evidence(
        spec: BugSpec,
        report: str,
        public_spec: str,
        answers: list[ClarificationAnswer],
    ) -> tuple[BugSpec, dict | None, list[dict]]:
        """원문 위치를 확인하고 유일하게 확인되는 출처만 보정한다.

        Args:
            spec: 모델이 반환한 제보 해석.
            report: 원래 제보.
            public_spec: 서비스 요구사항.
            answers: 사용자 답변 기록.

        Returns:
            확인된 해석, 출처 보정 내역, 일치한 원문 식별 정보.

        Raises:
            ValueError: 근거가 없거나 보정할 출처가 모호한 경우.

        Notes:
            질문은 검색 대상에 포함하지 않는다.
            기대 동작과 근거 문장 자체는 변경하지 않는다.
        """
        if spec.expectation_source == ExpectationSource.UNKNOWN:
            return spec, None, []

        evidence = normalize(
            spec.expectation_evidence or ""
        )

        if not evidence:
            raise ValueError("기대 동작의 근거가 비어 있습니다.")

        documents = [
            {
                "id": "report",
                "source": ExpectationSource.REPORT,
                "text": report,
            },
            {
                "id": "public_spec",
                "source": ExpectationSource.PUBLIC_SPEC,
                "text": public_spec,
            },
        ]

        documents.extend(
            {
                "id": f"answer:{index + 1}",
                "source": ExpectationSource.USER_ANSWER,
                "text": item.answer,
            }
            for index, item in enumerate(answers)
        )

        matches = [
            {
                "document_id": document["id"],
                "source": document["source"].value,
            }
            for document in documents
            if evidence in normalize(document["text"])
        ]

        selected_matches = [
            match
            for match in matches
            if match["source"]
            == spec.expectation_source.value
        ]

        # 모델이 지정한 출처에서 확인되면 보정하지 않는다.
        if selected_matches:
            return spec, None, selected_matches

        if not matches:
            raise ValueError(
                "어떤 원문에서도 기대 동작의 근거를 찾지 못했습니다. "
                f"반환 근거: {spec.expectation_evidence!r}"
            )

        matched_sources = {
            match["source"]
            for match in matches
        }

        if len(matched_sources) != 1:
            raise ValueError(
                "선택한 출처에는 근거가 없고, "
                "다른 여러 출처에 있어 보정 대상을 확정할 수 없습니다."
            )

        resolved_source = ExpectationSource(
            next(iter(matched_sources))
        )

        correction = {
            "from": spec.expectation_source.value,
            "to": resolved_source.value,
            "reason": "인용문이 다른 출처 한 종류에서만 확인됨",
        }

        corrected = spec.model_dump(mode="json")
        corrected["expectation_source"] = resolved_source.value

        return (
            BugSpec.model_validate(corrected),
            correction,
            matches,
        )

    @staticmethod
    def _save_diagnostics(
        path: Path,
        payload: dict,
        attempts: list[dict],
    ) -> None:
        """입력과 시도별 응답·검증·보정 내역을 저장한다.

        Args:
            path: 진단 파일 경로.
            payload: 제보와 사용자 답변.
            attempts: 시도별 결과.

        Returns:
            없음.

        Raises:
            OSError: 파일을 저장하지 못한 경우.
        """
        path.write_text(
            json.dumps(
                {
                    "input": payload,
                    "attempts": attempts,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _summarize_usage(attempts: list[dict]) -> dict:
        """모든 해석 시도의 토큰 사용량을 합산한다.

        Args:
            attempts: 시도별 모델 사용량 기록.

        Returns:
            모델 정보, 호출 횟수, 토큰 사용량.
        """
        last_usage = attempts[-1]["usage"]

        def total_tokens(key: str) -> int | None:
            values = [
                item["usage"].get(key)
                for item in attempts
            ]

            if all(type(value) is int for value in values):
                return sum(values)

            return None

        return {
            "backend": last_usage.get("backend"),
            "model": last_usage.get("model"),
            "model_calls": len(attempts),
            "input_tokens": total_tokens("input_tokens"),
            "output_tokens": total_tokens("output_tokens"),
        }