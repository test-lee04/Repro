"""재현 판정과 생성 테스트의 검증 상태를 구분한다."""

from enum import Enum


class ReproductionStatus(str, Enum):
    """재현 시도의 판정 상태.

    Notes:
        제한 시간 도달이나 관찰 부족은 미재현으로 확정하지 않는다.
        not_reproduced는 충분한 확인을 수행한 이번 실행의 결과다.
    """

    PENDING = "pending"
    REPRODUCED = "reproduced"
    NOT_REPRODUCED = "not_reproduced"
    INCONCLUSIVE = "inconclusive"
    NEEDS_INFORMATION = "needs_information"
    UNSUPPORTED = "unsupported"
    ENVIRONMENT_ERROR = "environment_error"


class TestStatus(str, Enum):
    """생성한 회귀 테스트의 상태.

    Notes:
        버그 탐지와 수정 후 정상 통과 확인을 구분한다.
        수정 환경이 없으면 검증 완료로 표시하지 않는다.
    """

    NOT_GENERATED = "not_generated"
    GENERATED = "generated"
    REPLAY_FAILED = "replay_failed"
    BUG_DETECTED = "bug_detected"
    AWAITING_FIX_VALIDATION = "awaiting_fix_validation"
    VALIDATED = "validated"
    UNSUPPORTED = "unsupported"
    ENVIRONMENT_ERROR = "environment_error"