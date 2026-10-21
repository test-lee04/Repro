"""구조화된 버그 제보에서 검증 조건 후보를 생성한다."""

import copy
import json
import time
import unicodedata

from app.agent.model_client import JsonModelClient
from app.agent.prompts import VERIFICATION_PROMPT
from app.agent.report_schema import (
    BugSpec,
    ClarificationAnswer,
    ExpectationSource,
)
from app.agent.verification_schema import VerificationPlan


def normalize_text(value: str) -> str:
    """원문 대조를 위해 문자 표현과 연속 공백을 정리한다.

    Args:
        value: 비교할 원문 또는 인용문.

    Returns:
        문자 표현과 연속 공백을 정리한 문자열.
    """
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split())


class VerificationPlanner:
    """제보별 검증 조건 후보를 생성하고 원문 근거를 검사한다."""

    def __init__(
        self,
        client: JsonModelClient,
        max_attempts: int = 2,
    ):
        """모델 연결과 최대 생성 횟수를 설정한다.

        Args:
            client: JSON 응답을 생성하는 모델 연결 객체.
            max_attempts: 첫 생성과 재시도를 합한 최대 호출 횟수.

        Raises:
            ValueError: 최대 생성 횟수가 1보다 작은 경우.
        """
        if max_attempts < 1:
            raise ValueError(
                "최대 생성 횟수는 1 이상이어야 합니다."
            )

        self.client = client
        self.max_attempts = max_attempts

    @staticmethod
    def _build_generation_schema() -> dict:
        """모델에 전달할 조건 생성 형식을 준비한다.

        Returns:
            검사 대상 설명을 필수로 지정한 JSON Schema.

        Notes:
            공통 조건 모델은 URL 검사에서 대상 설명 생략을 허용한다.
            생성 단계에서는 URL 검사도 현재 페이지 등의 설명을 받는다.
            공통 모델의 검증 규칙 자체는 변경하지 않는다.
        """
        schema = copy.deepcopy(
            VerificationPlan.model_json_schema()
        )

        condition_schema = schema["$defs"][
            "VerificationCondition"
        ]

        # 기본값이 있는 선택 필드는 모델이 생략하거나 null로
        # 반환할 수 있으므로 생성용 형식에서는 명확히 제한한다.
        condition_schema["properties"][
            "target_description"
        ] = {
            "type": "string",
            "minLength": 1,
            "description": (
                "제보에서 확인할 화면 대상의 설명. "
                "실제 요소 ID나 selector가 아닌 대상의 역할과 이름. "
                "URL 검사에서는 현재 페이지를 설명한다."
            ),
        }

        required = condition_schema.setdefault(
            "required",
            [],
        )

        if "target_description" not in required:
            required.append("target_description")

        return schema

    async def plan(
        self,
        spec: BugSpec,
        *,
        report: str,
        public_spec: str = "",
        answers: list[ClarificationAnswer] | None = None,
    ) -> dict:
        """제보에서 검증 조건 후보를 생성한다.

        Args:
            spec: 제보 해석기가 반환한 구조화 결과.
            report: 사용자의 원래 제보.
            public_spec: 사용자가 제공한 서비스 요구사항.
            answers: 이전 확인 질문에 대한 사용자 답변.

        Returns:
            조건 생성 결과, 시도 기록, 사용량과 소요 시간.

        Raises:
            ValueError: 모든 응답이 형식 또는 근거 검사에 실패한 경우.
        """
        started = time.perf_counter()
        answers = list(answers or [])

        # 해결되지 않은 질문이 있으면 조건 생성에 모델을 쓰지 않는다.
        if spec.needs_clarification:
            plan = VerificationPlan(
                status="needs_information",
                summary=(
                    "제보 해석 단계의 확인 질문에 "
                    "대한 답변이 필요합니다."
                ),
                conditions=[],
                questions=[
                    item.question
                    for item in spec.missing_information
                ],
                limitation=None,
            )

            return {
                "plan": plan,
                "attempts": [],
                "usage": {
                    "model_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
                "elapsed_seconds": (
                    time.perf_counter() - started
                ),
            }

        schema = self._build_generation_schema()

        payload = {
            "bug_spec": spec.model_dump(mode="json"),
            "report": report,
            "public_spec": public_spec,
            "answers": [
                answer.model_dump(mode="json")
                for answer in answers
            ],
            "output_schema": schema,
        }

        messages = [
            {
                "role": "system",
                "content": VERIFICATION_PROMPT,
            },
            {
                "role": "user",
                "content": json.dumps(
                    payload,
                    ensure_ascii=False,
                ),
            },
        ]

        attempts = []

        for attempt_number in range(
            1,
            self.max_attempts + 1,
        ):
            response = await self.client.generate(
                messages=messages,
                schema=schema,
            )

            content = response["content"]

            record = {
                "attempt": attempt_number,
                "content": content,
                "usage": response.get("usage") or {},
                "validation_error": None,
            }

            attempts.append(record)

            try:
                plan = VerificationPlan.model_validate_json(
                    content
                )

                # 모델에 전달한 형식 제약에만 의존하지 않는다.
                self._validate_targets(plan)

                self._validate_evidence(
                    plan,
                    report=report,
                    public_spec=public_spec,
                    answers=answers,
                )

            except ValueError as error:
                record["validation_error"] = str(error)

                print(
                    "검증 조건 응답 검사 실패 "
                    f"({attempt_number}/{self.max_attempts}): "
                    f"{error}"
                )

                if attempt_number == self.max_attempts:
                    raise ValueError(
                        "검증 조건이 형식 또는 원문 근거 검사를 "
                        f"통과하지 못했습니다: {error}"
                    ) from error

                # 실패한 응답과 검사 오류를 함께 전달한다.
                # 대상 설명이나 기대값을 코드에서 임의로 채우지 않는다.
                messages.extend(
                    [
                        {
                            "role": "assistant",
                            "content": content,
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "validation_error": str(error),
                                    "output_schema": schema,
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ]
                )

                continue

            return {
                "plan": plan,
                "attempts": attempts,
                "usage": self._summarize_usage(attempts),
                "elapsed_seconds": (
                    time.perf_counter() - started
                ),
            }

        raise RuntimeError(
            "검증 조건 생성이 정상적으로 종료되지 않았습니다."
        )

    @staticmethod
    def _validate_targets(
        plan: VerificationPlan,
    ) -> None:
        """생성된 모든 조건에 대상 설명이 있는지 확인한다.

        Args:
            plan: 모델이 생성한 조건 후보.

        Raises:
            ValueError: 대상 설명이 없거나 공백인 경우.
        """
        for condition in plan.conditions:
            description = condition.target_description

            if (
                description is None
                or not description.strip()
            ):
                raise ValueError(
                    f"조건 {condition.condition_id}의 "
                    "target_description에 검사할 화면 대상의 "
                    "설명이 필요합니다."
                )

    @staticmethod
    def _validate_evidence(
        plan: VerificationPlan,
        *,
        report: str,
        public_spec: str,
        answers: list[ClarificationAnswer],
    ) -> None:
        """각 조건의 근거가 지정한 원문에 있는지 검사한다.

        Args:
            plan: 검사할 조건 후보.
            report: 사용자의 원래 제보.
            public_spec: 사용자가 제공한 서비스 요구사항.
            answers: 이전 확인 질문에 대한 사용자 답변.

        Raises:
            ValueError: 지정한 출처에서 인용문을 찾을 수 없는 경우.

        Notes:
            원문 일치는 인용문이 실제로 존재하는지 검사한다.
            기대값의 의미가 올바른지까지 보장하지는 않는다.
        """
        documents = {
            ExpectationSource.REPORT: [report],
            ExpectationSource.PUBLIC_SPEC: [public_spec],
            ExpectationSource.USER_ANSWER: [
                answer.answer
                for answer in answers
            ],
        }

        for condition in plan.conditions:
            evidence = normalize_text(condition.evidence)

            source_documents = documents.get(
                condition.source,
                [],
            )

            matched = bool(evidence) and any(
                evidence in normalize_text(document)
                for document in source_documents
                if document.strip()
            )

            if not matched:
                raise ValueError(
                    f"조건 {condition.condition_id}의 근거가 "
                    "지정한 원문에 없습니다."
                )

    @staticmethod
    def _summarize_usage(
        attempts: list[dict],
    ) -> dict:
        """재시도를 포함한 모델 사용량을 합산한다.

        Args:
            attempts: 각 모델 호출의 응답과 검사 기록.

        Returns:
            모델 정보, 호출 횟수와 합산한 토큰 사용량.
        """
        usages = [
            attempt["usage"]
            for attempt in attempts
        ]

        last_usage = usages[-1] if usages else {}

        summary = {
            "backend": last_usage.get("backend"),
            "model": last_usage.get("model"),
            "model_calls": len(usages),
        }

        for key in ("input_tokens", "output_tokens"):
            values = [
                usage.get(key)
                for usage in usages
            ]

            summary[key] = (
                sum(values)
                if all(
                    type(value) is int
                    for value in values
                )
                else None
            )

        return summary