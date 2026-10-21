"""제보별 검증 조건 생성 결과를 정의한다."""

from typing import Literal

from pydantic import Field, model_validator

from app.agent.report_schema import StrictModel
from app.testing.check_schema import VerificationCondition


class VerificationPlan(StrictModel):
    """검증 조건 생성 결과를 표현한다.

    Attributes:
        status: 조건 후보 생성, 추가 정보 필요, 지원 범위 밖의 상태.
        summary: 결과를 설명하는 짧은 문장.
        conditions: 제보와 요구사항을 근거로 만든 검증 조건 후보.
        questions: 조건을 만들기 위해 사용자에게 확인할 질문.
        limitation: 현재 방식으로 검사하기 어려운 이유.

    Notes:
        candidate는 검증 조건 후보가 만들어졌다는 의미다.
        실제 화면 연결이나 버그 재현 성공을 의미하지 않는다.
    """

    status: Literal[
        "candidate",
        "needs_information",
        "unsupported",
    ]

    summary: str = Field(min_length=1)

    conditions: list[VerificationCondition] = Field(
        default_factory=list
    )

    questions: list[str] = Field(default_factory=list)

    limitation: str | None = Field(
        default=None,
        min_length=1,
    )

    @model_validator(mode="after")
    def validate_plan(self):
        """결과 상태와 포함된 데이터의 일관성을 확인한다.

        Returns:
            검증을 통과한 결과 객체.

        Raises:
            ValueError: 상태와 조건, 질문, 한계 설명이 모순되는 경우.
        """
        if any(not question.strip() for question in self.questions):
            raise ValueError(
                "확인 질문은 비어 있을 수 없습니다."
            )

        condition_ids = [
            condition.condition_id
            for condition in self.conditions
        ]

        if len(condition_ids) != len(set(condition_ids)):
            raise ValueError(
                "검증 조건의 식별자는 서로 달라야 합니다."
            )

        if self.status == "candidate":
            if not self.conditions:
                raise ValueError(
                    "조건 후보 상태에는 검증 조건이 필요합니다."
                )

            if self.questions:
                raise ValueError(
                    "필수 질문이 남아 있다면 "
                    "추가 정보 필요 상태를 사용해야 합니다."
                )

            if self.limitation is not None:
                raise ValueError(
                    "조건 후보 상태에는 지원 불가 사유를 넣지 않습니다."
                )

        elif self.status == "needs_information":
            if not self.questions:
                raise ValueError(
                    "추가 정보 필요 상태에는 확인 질문이 필요합니다."
                )

            # 필요한 정보가 없는 상태의 조건을 실행에 넘기지 않는다.
            if self.conditions:
                raise ValueError(
                    "추가 정보 필요 상태에는 "
                    "실행할 조건 후보를 넣지 않습니다."
                )

            if self.limitation is not None:
                raise ValueError(
                    "추가 정보 필요 상태에는 "
                    "지원 불가 사유를 넣지 않습니다."
                )

        elif self.status == "unsupported":
            if self.limitation is None:
                raise ValueError(
                    "지원 범위 밖의 상태에는 이유가 필요합니다."
                )

            if self.conditions or self.questions:
                raise ValueError(
                    "지원 범위 밖의 상태에는 "
                    "조건이나 확인 질문을 넣지 않습니다."
                )

        return self