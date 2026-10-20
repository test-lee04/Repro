"""Keep reproduction and test validity separate. Exhausted retries never imply success."""
from enum import Enum


class ReproductionStatus(str, Enum):
    PENDING = "pending"
    REPRODUCED = "reproduced"
    NOT_REPRODUCED = "not_reproduced"
    NEEDS_INFORMATION = "needs_information"
    ENVIRONMENT_ERROR = "environment_error"


class TestStatus(str, Enum):
    NOT_GENERATED = "not_generated"
    GENERATED = "generated"
    REPLAY_FAILED = "replay_failed"
    BUG_DETECTED = "bug_detected"
    VALIDATED = "validated"
    ENVIRONMENT_ERROR = "environment_error"
