"""제보에 따라 구성할 공통 검증 조건을 정의한다."""

from typing import Literal

from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from app.agent.report_schema import (
    ExpectationSource,
    StrictModel,
)


class VerificationCondition(StrictModel):
    """기대 결과를 검사 가능한 조건으로 표현한다.

    Attributes:
        condition_id: 조건을 구분하는 식별자.
        kind: 관찰하고 비교할 값의 종류.
        target_description: 확인할 화면 요소의 설명.
        operator: 비교 연산.
        expected_value: 근거가 있는 정상 기대값.
        source: 기대값의 출처.
        evidence: 출처에서 가져온 근거 문장.
        checkpoint: 조건을 확인할 시점이나 선행 행동 설명.

    Notes:
        이 모델은 조건의 형식과 필드 간 일관성을 검사한다.
        실제 요소 연결과 비교는 브라우저 검증 도구와 조건 비교기에서 수행한다.
    """

    condition_id: str = Field(min_length=1)

    kind: Literal[
        "presence",
        "text",
        "value",
        "url",
        "number",
    ]

    target_description: str | None = Field(
        default=None,
        min_length=1,
    )

    operator: Literal[
        "equals",
        "contains",
        "greater_than",
        "less_than",
    ]

    expected_value: (
        StrictBool
        | StrictInt
        | StrictFloat
        | StrictStr
    )

    source: ExpectationSource
    evidence: str = Field(min_length=1)
    checkpoint: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_condition(self):
        """조건 종류와 비교 방식의 조합을 검사한다.

        Returns:
            검증을 통과한 조건 객체.

        Raises:
            ValueError: 출처·대상·값·연산의 조합이 잘못된 경우.
        """
        if self.source == ExpectationSource.UNKNOWN:
            raise ValueError(
                "출처가 불명확한 기대값은 실행 조건으로 만들 수 없습니다."
            )

        if self.kind != "url" and self.target_description is None:
            raise ValueError(
                "현재 페이지 URL 검사를 제외하면 대상 설명이 필요합니다."
            )

        if self.kind == "presence":
            if (
                self.operator != "equals"
                or type(self.expected_value) is not bool
            ):
                raise ValueError(
                    "존재 여부는 equals와 참·거짓 값으로 검사합니다."
                )

        elif self.kind in {"text", "value", "url"}:
            if (
                self.operator not in {"equals", "contains"}
                or not isinstance(self.expected_value, str)
                or not self.expected_value.strip()
            ):
                raise ValueError(
                    "문자열 검사는 equals 또는 contains와 "
                    "비어 있지 않은 문자열을 사용합니다."
                )

        elif self.kind == "number":
            if (
                self.operator
                not in {"equals", "greater_than", "less_than"}
                or type(self.expected_value) not in {int, float}
            ):
                raise ValueError(
                    "수치 검사는 숫자와 지원되는 비교 연산을 사용합니다."
                )

            # 무한대나 숫자가 아닌 값은 기대 금액 등에 사용할 수 없다.
            if isinstance(self.expected_value, float):
                import math

                if not math.isfinite(self.expected_value):
                    raise ValueError(
                        "기대 수치는 유한한 숫자여야 합니다."
                    )

        return self