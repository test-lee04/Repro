"""자연어 버그 제보 해석 결과의 데이터 계약."""

from enum import Enum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


class StrictModel(BaseModel):
    # 정의하지 않은 필드가 섞이면 오류로 처리한다.
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )


class ExpectationSource(str, Enum):
    REPORT = "report"
    PUBLIC_SPEC = "public_spec"
    UNKNOWN = "unknown"


class ReportedInput(StrictModel):
    name: str = Field(min_length=1)
    value: str = Field(min_length=1)


class MissingInformation(StrictModel):
    field: str = Field(min_length=1)
    question: str = Field(min_length=1)


class BugSpec(StrictModel):
    summary: str = Field(min_length=1)

    # 제보자가 언급한 순서다. 실행 계획과 구분한다.
    reported_steps: list[str] = Field(default_factory=list)
    inputs: list[ReportedInput] = Field(default_factory=list)

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

    # 출처에서 가져온 근거 문장.
    expectation_evidence: str | None = Field(
        default=None,
        min_length=1,
    )

    missing_information: list[MissingInformation] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_expectation(self):
        if self.expectation_source == ExpectationSource.UNKNOWN:
            if (
                self.expected_behavior is not None
                or self.expectation_evidence is not None
            ):
                raise ValueError(
                    "출처를 모르는 기대 동작과 근거를 "
                    "임의로 채울 수 없습니다."
                )

            if not any(
                item.field == "expected_behavior"
                for item in self.missing_information
            ):
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
        return bool(self.missing_information)