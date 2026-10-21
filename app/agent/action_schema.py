"""AI가 선택할 행동과 결정의 데이터 형식을 정의한다."""

from typing import Literal

from pydantic import Field, model_validator

from app.agent.report_schema import StrictModel


class BrowserAction(StrictModel):
    """브라우저에서 수행할 행동 하나.

    Attributes:
        kind: 클릭, 입력 또는 현재 페이지 새로고침.
        element_id: 현재 관찰에 포함된 대상 식별자.
        value: 입력 행동에 사용할 문자열.

    Notes:
        새로고침도 관찰 목록의 현재 페이지 대상을 선택한다.
    """

    kind: Literal["click", "fill", "reload"]
    element_id: str = Field(min_length=1)
    value: str | None = None

    @model_validator(mode="after")
    def validate_value(self):
        """행동에 맞는 입력값인지 검사한다.

        Returns:
            검증을 통과한 행동.

        Raises:
            ValueError: 입력값이 행동 종류와 맞지 않는 경우.
        """
        if self.kind == "fill" and self.value is None:
            raise ValueError(
                "입력 행동에는 문자열 값이 필요합니다."
            )

        if self.kind != "fill" and self.value is not None:
            raise ValueError(
                "클릭과 새로고침에는 입력값을 넣지 않습니다."
            )

        return self


class NextDecision(StrictModel):
    """AI의 다음 행동, 정보 요청 또는 종료 결정."""

    decision: Literal[
        "act",
        "finish",
        "need_information",
    ]

    description: str = Field(min_length=1)
    action: BrowserAction | None = None

    question: str | None = Field(
        default=None,
        min_length=1,
    )

    @model_validator(mode="after")
    def validate_decision(self):
        """결정과 행동·질문의 조합을 검사한다.

        Returns:
            검증을 통과한 결정.

        Raises:
            ValueError: 결정에 필요한 정보가 없거나 모순되는 경우.
        """
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