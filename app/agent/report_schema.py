"""자연어 버그 제보와 추가 답변의 데이터 형식을 정의한다."""

from enum import Enum

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


class BugSpec(StrictModel):
    """모델이 구조화한 버그 제보.

    Attributes:
        summary: 제보 현상 요약.
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

    reported_steps: list[str] = Field(
        default_factory=list
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

        return self

    @property
    def needs_clarification(self) -> bool:
        """추가 질문이 남아 있는지 반환한다."""
        return bool(self.missing_information)