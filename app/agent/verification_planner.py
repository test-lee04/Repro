"""구조화된 버그 제보에서 검증 조건 후보를 생성한다."""

import copy
import json
import time
import unicodedata
from typing import Literal
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.model_client import JsonModelClient
from app.agent.prompts import VERIFICATION_PROMPT, SCOPE_REVIEW_PROMPT
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


class ComparisonReview(BaseModel):
    """원문에 비춰 기대 문구와 비교 강도가 적절한지 기록한다."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    status: Literal["consistent", "mismatch", "unclear"]
    evidence_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ScopeItem(BaseModel):
    """검증 조건 하나의 대상 범위 검토 결과를 정의한다."""

    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    condition_id: str = Field(min_length=1)
    status: Literal['same_scope', 'mismatch', 'unclear']
    evidence_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    suggested_target: str | None
    comparison: ComparisonReview

    @model_validator(mode='after')
    def validate_suggestion(self):
        """범위 불일치에만 수정 제안을 허용한다."""
        if self.status == 'mismatch':
            if not self.suggested_target or not self.suggested_target.strip():
                raise ValueError('범위 불일치에는 원문에 근거한 대상 제안이 필요합니다.')
        elif self.suggested_target is not None:
            raise ValueError('일치 또는 보류 상태에는 대상 제안을 넣지 않습니다.')
        return self


class ScopeReview(BaseModel):
    """모든 검증 조건을 한 번씩 검토한 결과다."""

    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    results: list[ScopeItem] = Field(min_length=1)


class ReviewResponseError(RuntimeError):
    """검토 응답 자체가 잘못돼 조건 재생성을 진행할 수 없음을 나타낸다."""


class VerificationPlanningError(ValueError):
    """실패한 전체 시도와 진단 경로를 호출자에게 보존한다."""

    def __init__(self, message, attempts, diagnostic_path):
        super().__init__(message)
        self.attempts = attempts
        self.diagnostic_path = str(diagnostic_path)


class VerificationPlanner:
    """제보별 검증 조건 후보를 생성하고 원문 근거를 검사한다."""

    def __init__(
        self,
        client: JsonModelClient,
        max_attempts: int = 2,
        max_review_attempts: int = 2,
    ):
        """모델 연결과 최대 생성 횟수를 설정한다.

        Args:
            client: JSON 응답을 생성하는 모델 연결 객체.
            max_attempts: 최초 조건 생성과 조건 재생성을 합한 최대 횟수.
            max_review_attempts: 조건 하나의 검토 응답 형식 오류를 재시도할 최대 횟수.

        Raises:
            ValueError: 최대 생성 횟수가 1보다 작은 경우.
        """
        if max_attempts < 1 or max_review_attempts < 1:
            raise ValueError(
                "최대 생성 횟수는 1 이상이어야 합니다."
            )

        self.client = client
        self.max_attempts = max_attempts
        self.max_review_attempts = max_review_attempts

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
                "URL 검사에서는 현재 페이지를 설명한다. "
                "원문이 영역이나 목록 전체를 지정하면 그 전체를 유지하고 "
                "임의의 항목이나 존재를 가정한 자식 요소로 좁히지 않는다."
            ),
        }

        condition_schema['properties']['operator']['description'] = (
            '표시 영역에 문구가 있어야 한다는 text 요구는 contains다. '
            '전체 문자열 또는 입력값이 정확히 같아야 하는 요구는 equals다. '
            '원문이 요구한 비교의 강도를 임의로 바꾸지 않는다.'
        )

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
        diagnostic_path = Path("artifacts") / "verification-planner" / f"{uuid4().hex}.json"

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
                if plan.status == 'candidate':
                    await self._review_scope(plan, record)


            except Exception as error:
                record["validation_error"] = str(error)

                print(
                    "검증 조건 응답 검사 실패 "
                    f"({attempt_number}/{self.max_attempts}): "
                    f"{error}"
                )

                self._save_diagnostics(diagnostic_path, payload, attempts, "failed_attempt")
                if attempt_number == self.max_attempts or not isinstance(error, ValueError):
                    raise VerificationPlanningError(
                        f"검증 조건 준비에 실패했습니다: {error}. 기록: {diagnostic_path}",
                        attempts, diagnostic_path,
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

            self._save_diagnostics(diagnostic_path, payload, attempts, "completed")
            return {
                "diagnostic_path": str(diagnostic_path),
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
    def _save_diagnostics(path, payload, attempts, status):
        """성공과 실패 모두에서 생성 및 검토의 원래 응답을 저장한다."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"status": status, "input": payload, "attempts": attempts},
                                   ensure_ascii=False, indent=2), encoding="utf-8")

    async def _review_scope(self, plan, record):
        """대상 범위와 비교 의미를 검토하고 응답 오류만 같은 단계에서 재시도한다.

        Args:
            plan: 원문 근거 검사를 통과한 조건 후보.
            record: 생성 호출과 모든 검토 호출을 보존할 기록.

        Raises:
            ReviewResponseError: 검토 형식이나 근거 참조를 끝내 확인하지 못한 경우.
            ValueError: 유효한 검토가 생성 조건의 불일치나 불명확함을 지적한 경우.

        Notes:
            실제 값은 전달하지 않는다. 근거는 모델이 재작성하지 않고 식별자로 참조한다.
            형식 오류는 검토기만 재시도하고 조건 의미 오류만 생성기에 돌려보낸다.
        """
        conditions = [item for item in plan.conditions if item.kind == "text"]
        if not conditions:
            return
        documents = {f"evidence-{number}": item for number, item in enumerate(conditions, 1)}
        by_condition = {item.condition_id: key for key, item in documents.items()}
        schema = ScopeReview.model_json_schema()
        # 각 조건과 근거를 같은 항목에 묶어 식별자 재선택의 부담을 줄인다.
        payload = {"conditions": [
            {"condition_id": item.condition_id, "target_description": item.target_description,
             "evidence_id": key, "evidence": item.evidence, "kind": item.kind,
             "operator": item.operator, "expected_value": item.expected_value}
            for key, item in documents.items()
        ], "output_schema": schema}
        schema["$defs"]["ScopeItem"]["properties"]["condition_id"]["enum"] = list(by_condition)
        for name in ("ScopeItem", "ComparisonReview"):
            schema["$defs"][name]["properties"]["evidence_id"]["enum"] = list(documents)
        messages = [
            {"role": "system", "content": SCOPE_REVIEW_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        scope_record = {"attempts": [], "review": None, "validation_error": None,
                        "evidence_catalog": {key: {"condition_id": item.condition_id,
                                                    "evidence": item.evidence}
                                             for key, item in documents.items()}}
        record["scope_review"] = scope_record
        for number in range(1, self.max_review_attempts + 1):
            response = await self.client.generate(messages=messages, schema=schema)
            attempt = {"attempt": number, "content": response["content"],
                       "usage": response.get("usage") or {}, "validation_error": None}
            scope_record["attempts"].append(attempt)
            try:
                review = ScopeReview.model_validate_json(response["content"])
                ids = [item.condition_id for item in review.results]
                if len(ids) != len(by_condition) or set(ids) != set(by_condition):
                    raise ValueError("모든 조건을 중복 없이 한 번씩 검토해야 합니다.")
                for item in review.results:
                    evidence_id = by_condition[item.condition_id]
                    if item.evidence_id != evidence_id or item.comparison.evidence_id != evidence_id:
                        raise ValueError(f"조건 {item.condition_id}에 배정한 근거 식별자를 그대로 사용해야 합니다.")
            except ValueError as error:
                attempt["validation_error"] = str(error)
                scope_record["validation_error"] = str(error)
                if number == self.max_review_attempts:
                    raise ReviewResponseError("검토 응답의 형식 또는 근거 참조를 확인하지 못했습니다.") from error
                messages.extend([
                    {"role": "assistant", "content": response["content"]},
                    {"role": "user", "content": json.dumps({
                        "validation_error": str(error),
                        "instruction": "조건과 근거는 변경하지 말고 검토 응답 전체만 수정하세요.",
                        "output_schema": schema,
                    }, ensure_ascii=False)},
                ])
                continue
            scope_record["review"] = review.model_dump(mode="json")
            scope_record["validation_error"] = None
            issues = []
            for item in review.results:
                if item.status != "same_scope" or item.comparison.status != "consistent":
                    issues.append({"condition_id": item.condition_id,
                                   "scope_status": item.status, "reason": item.reason,
                                   "comparison_status": item.comparison.status,
                                   "comparison_reason": item.comparison.reason,
                                   "source_evidence": documents[item.evidence_id].evidence,
                                   "suggested_target": item.suggested_target})
            if issues:
                # 근거 문구는 검증한 원본에서 코드로 복원하며 모델의 제안은 자동 적용하지 않는다.
                raise ValueError("원문과 검사 범위 또는 비교 조건을 다시 맞추세요. "
                                 + json.dumps(issues, ensure_ascii=False))
            return

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
        usages = []
        for attempt in attempts:
            usages.append(attempt['usage'])
            if attempt.get('scope_review') is not None:
                usages.extend(item['usage'] for item in attempt['scope_review']['attempts'])

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