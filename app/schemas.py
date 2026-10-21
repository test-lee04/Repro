"""서비스 실행 요청과 대상 서비스 정보를 정의한다."""

from enum import Enum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StrictBool,
    model_validator,
)


class RunMode(str, Enum):
    ONE_SHOT = "one_shot"
    AGENT = "agent"


class TargetService(BaseModel):
    """테스트할 웹서비스 정보를 표현한다.

    Attributes:
        start_url: 테스트를 시작할 주소.
        description: 서비스나 시작 화면에 관한 설명.
        public_spec: 사용자가 제공한 공개 요구사항.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    start_url: HttpUrl
    description: str | None = Field(
        default=None,
        min_length=1,
    )
    public_spec: str | None = Field(
        default=None,
        min_length=1,
    )


class RunRequest(BaseModel):
    """제보와 테스트 대상을 전달받는다.

    Attributes:
        report: 사용자의 자연어 버그 제보.
        target: 테스트할 서비스 정보.
        target_id: 기존에 등록된 대상의 식별자.
        mode: 단발 계획 또는 Agent 실행 모드.

    Notes:
        target_id는 기존 실행 방식과의 전환을 위해 유지한다.
        target과 target_id 중 하나만 지정한다.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    report: str = Field(
        min_length=1,
        max_length=10000,
    )
    target: TargetService | None = None
    target_id: str | None = Field(
        default=None,
        min_length=1,
    )
    mode: RunMode = RunMode.AGENT

    @model_validator(mode="after")
    def validate_target(self):
        """테스트 대상이 하나로 지정됐는지 확인한다.

        Returns:
            검증을 통과한 요청 객체.

        Raises:
            ValueError: 대상이 없거나 두 방식이 함께 지정된 경우.
        """
        if self.target is None and self.target_id is None:
            raise ValueError(
                "target 또는 target_id가 필요합니다."
            )

        if self.target is not None and self.target_id is not None:
            raise ValueError(
                "target과 target_id 중 하나만 지정하세요."
            )

        return self


class AnswersRequest(BaseModel):
    """추가 질문에 대한 사용자 답변을 전달한다."""

    model_config = ConfigDict(extra="forbid")

    answers: dict[str, str]
    reparse_report: StrictBool = False