"""자연어 버그 제보와 추가 답변의 데이터 형식을 정의한다."""

from enum import Enum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


class StrictModel(BaseModel):
    """정의하지 않은 필드를 거부하는 공통 모델."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )


class ExpectationSource(str, Enum):
    """정상 기대 동작의 출처."""

    REPORT = "report"
    PUBLIC_SPEC = "public_spec"
    USER_ANSWER = "user_answer"
    UNKNOWN = "unknown"


class ClarificationAnswer(StrictModel):
    """확인 질문과 사용자의 답변을 함께 기록한다.

    Attributes:
        question: 사용자에게 실제로 제시한 질문.
        answer: 사용자가 입력한 답변.

    Notes:
        질문 자체는 정상 기대 동작의 근거가 아니다.
        사용자의 답변 내용만 추가 근거로 사용한다.
    """

    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)


class ReportedInput(StrictModel):
    """제보나 사용자 답변에 명시된 입력값.

    Attributes:
        name: 입력 대상과 역할을 설명하는 이름.
        value: 사용자가 제공한 값.

    Notes:
        특정 서비스의 필드 이름을 강제하지 않는다.
    """

    name: str = Field(min_length=1)
    value: str = Field(min_length=1)


class MissingInformation(StrictModel):
    """제보 해석에 부족한 정보와 확인 질문."""

    field: str = Field(min_length=1)
    question: str = Field(min_length=1)


class ReportedPrecondition(StrictModel):
    """제보자가 밝힌 초기 상태를 행동이나 실제 검증 결과와 구분해 보존한다.

    Attributes:
        description: 브라우저 조작 전의 상태에 대한 설명.
        source: 실제 상태를 알려 준 원래 제보 또는 사용자 답변.
        evidence: 해당 출처에서 그대로 가져온 연속된 근거.

    Notes:
        이 자료는 사용자가 보고한 조건이며 실제 화면에서 확인한 사실은 아니다.
        요구사항 문서만 보고 실제 초기 상태를 가정할 수 없다.
    """

    description: str = Field(min_length=1)
    source: Literal["report", "user_answer"]
    evidence: str = Field(min_length=1)


class BugSpec(StrictModel):
    """모델이 구조화한 버그 제보.

    Attributes:
        summary: 제보 현상 요약.
        preconditions: 사용자가 명시한 초기 상태. 실제 화면 검증 결과가 아니다.
        reported_steps: 사용자가 언급한 조작 순서.
        inputs: 사용자가 명시한 입력값.
        actual_behavior: 사용자가 보고한 실제 동작.
        expected_behavior: 근거가 있는 정상 기대 동작.
        expectation_source: 기대 동작의 출처.
        expectation_evidence: 출처에서 가져온 근거 문장.
        missing_information: 부족한 정보와 확인 질문.

    Notes:
        보고된 실제 동작은 브라우저에서 확인한 사실이 아니다.
        보고된 순서도 아직 검증된 재현 절차는 아니다.
    """

    summary: str = Field(min_length=1)

    preconditions: list[ReportedPrecondition] = Field(
        default_factory=list,
        description="제보나 실제 답변에 명시한 초기 상태. 행동 목록과 실제 관찰 결과가 아니다.",
    )

    reported_steps: list[str] = Field(
        default_factory=list,
        description="입력, 클릭, 저장, 새로고침 등 실제 조작만 순서대로 기록한다. 이미 주어진 상태 설명은 preconditions에 기록한다.",
    )

    inputs: list[ReportedInput] = Field(
        default_factory=list
    )

    actual_behavior: str | None = Field(
        default=None,
        min_length=1,
    )

    expected_behavior: str | None = Field(
        default=None,
        min_length=1,
        description=(
            '원문에 근거해 확정한 정상 기대 동작. 불명확하거나 충돌하면 null이다. '
            '값을 확정했다면 같은 expected_behavior를 missing_information에 넣지 않는다.'
        ),
    )

    expectation_source: ExpectationSource = (
        ExpectationSource.UNKNOWN
    )

    expectation_evidence: str | None = Field(
        default=None,
        min_length=1,
    )

    missing_information: list[MissingInformation] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_expectation(self):
        """기대 동작과 출처 정보의 일관성을 검사한다.

        Returns:
            검증을 통과한 제보 객체.

        Raises:
            ValueError: 기대 동작·근거·확인 질문이 모순되는 경우.
        """
        if self.expectation_source == ExpectationSource.UNKNOWN:
            if (
                self.expected_behavior is not None
                or self.expectation_evidence is not None
            ):
                raise ValueError(
                    "출처를 모르는 기대 동작과 근거를 "
                    "임의로 채울 수 없습니다."
                )

            has_question = any(
                item.field == "expected_behavior"
                for item in self.missing_information
            )

            if not has_question:
                raise ValueError(
                    "기대 동작이 불명확하면 확인 질문이 필요합니다."
                )

        else:
            if (
                self.expected_behavior is None
                or self.expectation_evidence is None
            ):
                raise ValueError(
                    "기대 동작에는 내용과 출처 근거가 필요합니다."
                )
            if any(item.field == 'expected_behavior' for item in self.missing_information):
                raise ValueError(
                    '정상 기대 동작과 근거를 확정하면서 expected_behavior를 '
                    '정보 부족으로 표시할 수 없습니다. 기대 동작을 알고 있다면 '
                    '실제로 그렇게 동작하는지는 브라우저에서 확인합니다. '
                    '기대 자체가 불명확하거나 충돌한다면 expected_behavior와 '
                    'expectation_evidence를 null, 출처를 unknown으로 두고 질문하세요.'
                )

        return self

    @property
    def needs_clarification(self) -> bool:
        """추가 질문이 남아 있는지 반환한다."""
        return bool(self.missing_information)