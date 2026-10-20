"""AI가 선택할 다음 행동의 형식과 검증 규칙."""

from typing import Literal

from pydantic import Field, model_validator

from app.agent.report_schema import StrictModel


class BrowserAction(StrictModel):
    kind: Literal["click", "fill"]
    element_id: str = Field(min_length=1)
    value: str | None = None

    @model_validator(mode="after")
    def validate_value(self):
        if self.kind == "fill" and self.value is None:
            raise ValueError(
                "입력 행동에는 문자열 값이 필요합니다."
            )

        if self.kind == "click" and self.value is not None:
            raise ValueError(
                "클릭 행동에는 입력값을 넣지 않습니다."
            )

        return self


class NextDecision(StrictModel):
    decision: Literal[
        "act",
        "finish",
        "need_information",
    ]

    # 사용자에게 보여줄 짧은 행동 목적이다.
    # 모델의 내부 추론 과정을 요청하지 않는다.
    description: str = Field(min_length=1)

    action: BrowserAction | None = None
    question: str | None = Field(
        default=None,
        min_length=1,
    )

    @model_validator(mode="after")
    def validate_decision(self):
        if self.decision == "act":
            if self.action is None:
                raise ValueError(
                    "행동 결정에는 action이 필요합니다."
                )
            if self.question is not None:
                raise ValueError(
                    "행동 결정에는 질문을 넣지 않습니다."
                )

        elif self.decision == "need_information":
            if self.question is None:
                raise ValueError(
                    "정보 요청에는 질문이 필요합니다."
                )
            if self.action is not None:
                raise ValueError(
                    "정보 요청에는 행동을 넣지 않습니다."
                )

        else:
            if self.action is not None or self.question is not None:
                raise ValueError(
                    "종료 결정에는 행동과 질문을 넣지 않습니다."
                )

        return self