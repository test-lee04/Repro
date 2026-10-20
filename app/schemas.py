from enum import Enum
from pydantic import BaseModel, Field


class RunMode(str, Enum):
    ONE_SHOT = "one_shot"
    AGENT = "agent"


class RunRequest(BaseModel):
    report: str = Field(min_length=1, max_length=10000)
    target_id: str = "demo_cart"
    mode: RunMode = RunMode.AGENT


class AnswersRequest(BaseModel):
    answers: dict[str, str]
