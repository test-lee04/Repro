"""제보의 행동 순서와 실제 실행 기록을 대조해 검사 시점 도달 여부를 확인한다."""

import json
from typing import Literal

from pydantic import Field, StrictInt, model_validator

from app.agent.model_client import JsonModelClient
from app.agent.prompts import CHECKPOINT_PROMPT
from app.agent.report_schema import BugSpec, StrictModel
from app.testing.check_schema import VerificationCondition


class StepCoverage(StrictModel):
    """References to recorded actions that support one reported step."""

    step_number: StrictInt = Field(ge=1)
    action_numbers: list[StrictInt]
    reason: str = Field(min_length=1)


class CheckpointReview(StrictModel):
    """A model review with references, not proof of internal application state."""

    status: Literal["reached", "inconclusive"]
    coverage: list[StepCoverage]
    checkpoint_action_numbers: list[StrictInt]
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_status(self):
        """Require supporting references for a reached decision."""
        if self.status == "reached":
            if not self.coverage or not self.checkpoint_action_numbers:
                raise ValueError("검사 시점 확인에는 행동 근거가 필요합니다.")
        return self


class CheckpointValidationError(ValueError):
    """Preserve failed responses for the workflow artifact."""

    def __init__(self, message, attempts):
        super().__init__(message)
        self.attempts = attempts


class CheckpointReviewer:
    """Assess execution coverage without comparing expected and actual values."""

    def __init__(self, client: JsonModelClient, max_attempts: int = 2):
        if max_attempts < 1:
            raise ValueError("최대 확인 횟수는 양수여야 합니다.")
        self.client = client
        self.max_attempts = max_attempts

    @staticmethod
    def validate_references(review, steps, history):
        """Check coverage, successful action references, and step ordering."""
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
        """Review the condition checkpoint using real execution history.

        Args:
            spec: Parsed user report.
            condition: Condition whose checkpoint needs review.
            loop_result: Result returned by the existing ReproductionLoop.

        Returns:
            Review and original model attempts.

        Raises:
            CheckpointValidationError: Responses fail reference validation.
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
            "checkpoint": condition.checkpoint,
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
