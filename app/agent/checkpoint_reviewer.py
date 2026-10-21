"""제보의 행동 순서와 실제 실행 기록을 대조해 검사 시점 도달 여부를 확인한다."""

import json
from typing import Literal

from pydantic import Field, StrictInt, model_validator

from app.agent.model_client import JsonModelClient
from app.agent.prompts import CHECKPOINT_PROMPT
from app.agent.report_schema import BugSpec, StrictModel
from app.testing.check_schema import VerificationCondition


class StepCoverage(StrictModel):
    """제보 단계 하나를 뒷받침하는 실제 행동 번호를 기록한다."""

    step_number: StrictInt = Field(ge=1)
    action_numbers: list[StrictInt]
    reason: str = Field(min_length=1)


class CheckpointReview(StrictModel):
    """내부 저장 성공과 구분해 실제 행동 근거를 검토한다."""

    status: Literal["reached", "inconclusive"]
    coverage: list[StepCoverage]
    checkpoint_action_numbers: list[StrictInt]
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_status(self):
        """완료 판정에 필요한 행동 참조를 확인한다."""
        if self.status == "reached":
            if not self.coverage or not self.checkpoint_action_numbers:
                raise ValueError("검사 시점 확인에는 행동 근거가 필요합니다.")
        return self


class CheckpointValidationError(ValueError):
    """검토에 실패한 응답을 실행 기록에 보존한다."""

    def __init__(self, message, attempts):
        super().__init__(message)
        self.attempts = attempts


class CheckpointReviewer:
    """기대값과 실제 값 비교와 별도로 실행 범위를 확인한다."""

    def __init__(self, client: JsonModelClient, max_attempts: int = 2):
        if max_attempts < 1:
            raise ValueError("최대 확인 횟수는 양수여야 합니다.")
        self.client = client
        self.max_attempts = max_attempts

    @staticmethod
    def validate_references(review, steps, history):
        """단계 범위, 성공한 행동 참조와 순서를 검사한다."""
        if review.status != "reached":
            return

        numbers = [item.step_number for item in review.coverage]
        if sorted(numbers) != list(range(1, len(steps) + 1)):
            raise ValueError("모든 제보 행동을 중복 없이 확인해야 합니다.")

        def validate_actions(action_numbers):
            if not action_numbers or action_numbers != sorted(set(action_numbers)):
                raise ValueError("행동 근거는 중복 없이 순서대로 지정해야 합니다.")
            for number in action_numbers:
                if number < 1 or number > len(history):
                    raise ValueError("실행 기록에 없는 행동 번호입니다.")
                if history[number - 1].get("success") is not True:
                    raise ValueError("실패한 행동은 완료 근거로 사용할 수 없습니다.")

        previous = 0
        for item in sorted(review.coverage, key=lambda entry: entry.step_number):
            validate_actions(item.action_numbers)
            if item.action_numbers[0] <= previous:
                raise ValueError("제보 순서가 다르거나 같은 행동 근거를 재사용했습니다.")
            previous = item.action_numbers[-1]

        validate_actions(review.checkpoint_action_numbers)
        if review.checkpoint_action_numbers[-1] != previous:
            raise ValueError("검사 시점 근거에 마지막 제보 행동이 포함돼야 합니다.")
        if previous != len(history):
            raise ValueError("마지막 제보 행동 이후의 조작이 있어 최종 검사 시점을 확정하지 않습니다.")

    async def review(
        self, spec: BugSpec, condition: VerificationCondition, loop_result: dict
    ) -> dict:
        """실제 행동 기록으로 검증 조건의 확인 시점을 검토한다.

        Args:
            spec: 구조화된 사용자 제보.
            condition: 확인 시점을 검토할 조건.
            loop_result: 기존 행동 반복기가 반환한 결과.

        Returns:
            검토 결과와 실제 모델 호출 기록.

        Raises:
            CheckpointValidationError: 행동 참조 검증을 통과하지 못한 경우.
        """
        history = loop_result.get("history", [])
        if loop_result.get("loop_status") != "finish_selected" or not history or not spec.reported_steps:
            return {
                "review": CheckpointReview(
                    status="inconclusive", coverage=[], checkpoint_action_numbers=[],
                    reason="행동 종료, 실행 기록 또는 제보의 행동 순서가 충분하지 않습니다.",
                ),
                "attempts": [],
            }

        return await self._review_actions(spec, condition.checkpoint, history)

    async def review_completion(self, spec: BugSpec, history: list[dict]) -> dict:
        """추가 조작 전에 제보의 행동이 실제 기록으로 완료됐는지 확인한다."""
        if not history or not spec.reported_steps:
            return {'review': CheckpointReview(status='inconclusive', coverage=[], checkpoint_action_numbers=[],
                                               reason='완료를 확인할 제보 단계 또는 실행 기록이 없습니다.'),
                    'attempts': []}
        return await self._review_actions(
            spec, '제보에 명시된 모든 조작을 수행한 직후. 정상 결과를 만들기 위한 추가 조작은 포함하지 않습니다.', history)

    async def _review_actions(self, spec, checkpoint, history):
        """종료 검토와 최종 검사 시점 검토에서 같은 근거 검증을 재사용한다."""
        actions = []
        for number, item in enumerate(history, 1):
            action = item["action"]
            target = next((element for element in item.get("before_elements", [])
                           if element.get("element_id") == action.get("element_id")), {})
            actions.append({
                "action_number": number,
                "kind": action.get("kind"),
                "entered_value": action.get("value"),
                "target_name": target.get("name", ""),
                "target_tag": target.get("tag", ""),
                "success": item.get("success") is True,
                "error_type": item.get("error_type"),
            })

        schema = CheckpointReview.model_json_schema()
        payload = {
            "reported_steps": [
                {"step_number": number, "description": step}
                for number, step in enumerate(spec.reported_steps, 1)
            ],
            "checkpoint": checkpoint,
            "actions": actions,
            "output_schema": schema,
        }
        messages = [
            {"role": "system", "content": CHECKPOINT_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        attempts = []
        for number in range(1, self.max_attempts + 1):
            response = await self.client.generate(messages=messages, schema=schema)
            record = {"attempt": number, "content": response["content"],
                      "usage": response.get("usage") or {}, "validation_error": None}
            attempts.append(record)
            try:
                review = CheckpointReview.model_validate_json(response["content"])
                self.validate_references(review, spec.reported_steps, history)
            except ValueError as error:
                record["validation_error"] = str(error)
                if number == self.max_attempts:
                    raise CheckpointValidationError(str(error), attempts) from error
                messages.extend([
                    {"role": "assistant", "content": response["content"]},
                    {"role": "user", "content": json.dumps(
                        {"validation_error": str(error), "output_schema": schema},
                        ensure_ascii=False,
                    )},
                ])
                continue
            return {"review": review, "attempts": attempts}

        raise RuntimeError("검사 시점 확인이 정상 종료되지 않았습니다.")
