"""저장된 실행 기록과 제보 현상을 대조해 재현 상태를 판정한다."""

import json
from typing import Literal

from pydantic import ConfigDict, Field, StrictStr, model_validator

from app.agent.checkpoint_reviewer import CheckpointReview, CheckpointReviewer
from app.agent.prompts import SYMPTOM_PROMPT
from app.agent.report_schema import BugSpec, StrictModel
from app.agent.verification_schema import VerificationPlan
from app.testing.condition_comparator import compare_condition
from app.testing.verdict import ReproductionStatus


class SymptomEvidence(StrictModel):
    """제보 현상 판단에 사용한 조건과 실제 관찰값."""

    model_config = ConfigDict(str_strip_whitespace=False)
    condition_id: str = Field(min_length=1)
    observed_value: StrictStr


class SymptomReview(StrictModel):
    """기대값 비교와 분리한 제보 현상 일치 판단."""

    match: Literal["matched", "not_matched", "uncertain"]
    evidence: list[SymptomEvidence]
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence(self):
        """확정 판단에는 실제 관찰 근거를 요구한다."""
        if self.match != "uncertain" and not self.evidence:
            raise ValueError("제보 현상 판단에는 관찰 근거가 필요합니다.")
        ids = [item.condition_id for item in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("같은 조건의 근거를 중복하지 않습니다.")
        return self


class ReproductionAssessor:
    """실행 근거를 먼저 검사하고 제보 현상 일치를 확인한다."""

    def __init__(self, client, max_attempts=2):
        if max_attempts < 1:
            raise ValueError("최대 시도 횟수는 양수여야 합니다.")
        self.client = client
        self.max_attempts = max_attempts

    async def assess(self, run: dict) -> dict:
        """기록된 실행의 최종 재현 상태를 반환한다.

        Args:
            run: workflow_result.json의 내용.

        Returns:
            상태, 이유, 다시 계산한 조건 비교와 모델 시도 기록.

        Notes:
            현재 웹사이트를 다시 검사하는 함수는 아니다.
            모델의 의미 판단은 완전한 증명이 아니며 근거와 함께 저장한다.
        """
        result = {"status": ReproductionStatus.INCONCLUSIVE.value,
                  "reason": "실행 근거가 충분하지 않습니다.",
                  "comparisons": [], "symptom_review": None, "attempts": []}

        def finish(status, reason):
            result.update(status=status.value, reason=reason)
            return result

        state = run.get("workflow_status")
        if state == "needs_information":
            return finish(ReproductionStatus.NEEDS_INFORMATION, "추가 답변이 필요합니다.")
        if state == "unsupported":
            return finish(ReproductionStatus.UNSUPPORTED, "현재 실행 방식의 지원 범위 밖입니다.")
        if state == "tool_error":
            return finish(ReproductionStatus.ENVIRONMENT_ERROR, "브라우저 조작 오류로 확인을 마치지 못했습니다.")
        if state != "conditions_checked":
            return finish(ReproductionStatus.INCONCLUSIVE, "조건 검사를 끝내지 못한 실행입니다.")

        try:
            spec = BugSpec.model_validate(run["interpretation"]["spec"])
            plan = VerificationPlan.model_validate(run["condition_generation"]["plan"])
            execution = run["execution"]
            checks = run["checks"]
            if execution.get("loop_status") != "finish_selected" or plan.status != "candidate":
                raise ValueError("실행 종료와 조건 생성 상태를 확인할 수 없습니다.")
            if spec.needs_clarification:
                return finish(ReproductionStatus.NEEDS_INFORMATION, "해결하지 않은 확인 질문이 있습니다.")
            if not spec.actual_behavior:
                return finish(ReproductionStatus.NEEDS_INFORMATION, "대조할 제보의 실제 현상이 없습니다.")
            index = {check["condition"]["condition_id"]: check for check in checks}
            expected_ids = {condition.condition_id for condition in plan.conditions}
            if len(index) != len(checks) or set(index) != expected_ids:
                raise ValueError("생성한 조건과 검사 기록의 목록이 다릅니다.")

            observations = []
            for condition in plan.conditions:
                check = index[condition.condition_id]
                if check["condition"] != condition.model_dump(mode="json"):
                    raise ValueError("검사 기록의 조건이 생성한 조건과 다릅니다.")
                review = CheckpointReview.model_validate(check["checkpoint"]["review"])
                CheckpointReviewer.validate_references(
                    review, spec.reported_steps, execution["history"]
                )
                comparison = compare_condition(
                    condition, check.get("binding_result"), check.get("read_result"),
                    checkpoint_reached=review.status == "reached",
                )
                result["comparisons"].append(comparison.model_dump(mode="json"))
                if comparison.status not in {"satisfied", "unsatisfied"}:
                    status = (ReproductionStatus.UNSUPPORTED if comparison.status == "unsupported"
                              else ReproductionStatus.INCONCLUSIVE)
                    return finish(status, "필요한 조건 비교를 확정할 수 없습니다.")
                observation = check["observation"]
                if observation["observation_id"] != comparison.observation_id:
                    raise ValueError("관찰과 비교 기록이 일치하지 않습니다.")
                selected = [element for element in observation["elements"]
                            if element["element_id"] == comparison.element_id]
                if len(selected) != 1:
                    raise ValueError("읽은 대상을 관찰에서 하나로 확인할 수 없습니다.")
                value_key = "value" if condition.kind == "value" else "text"
                if selected[0].get(value_key) != comparison.actual_value:
                    raise ValueError("관찰 기록과 실제 읽은 값이 다릅니다.")
                observations.append({"condition_id": condition.condition_id,
                                     "target_name": selected[0].get("name", ""),
                                     "kind": condition.kind,
                                     "actual_value": comparison.actual_value})
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            return finish(ReproductionStatus.INCONCLUSIVE, f"실행 근거 검사를 통과하지 못했습니다: {error}")

        # 정상 기대값과 조건 충족 상태는 현상 대조 모델에 전달하지 않는다.
        schema = SymptomReview.model_json_schema()
        payload = {"reported_actual_behavior": spec.actual_behavior,
                   "observations": observations, "output_schema": schema}
        messages = [{"role": "system", "content": SYMPTOM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        actual_values = {item["condition_id"]: item["actual_value"] for item in observations}

        for number in range(1, self.max_attempts + 1):
            response = await self.client.generate(messages=messages, schema=schema)
            record = {"attempt": number, "content": response["content"],
                      "usage": response.get("usage") or {}, "validation_error": None}
            result["attempts"].append(record)
            try:
                review = SymptomReview.model_validate_json(response["content"])
                for evidence in review.evidence:
                    if evidence.condition_id not in actual_values:
                        raise ValueError("검사하지 않은 조건을 근거로 사용했습니다.")
                    if evidence.observed_value != actual_values[evidence.condition_id]:
                        raise ValueError("실제 읽은 값과 모델의 인용값이 다릅니다.")
            except ValueError as error:
                record["validation_error"] = str(error)
                if number == self.max_attempts:
                    return finish(ReproductionStatus.INCONCLUSIVE, "현상 대조 응답의 근거를 확인하지 못했습니다.")
                messages.extend([
                    {"role": "assistant", "content": response["content"]},
                    {"role": "user", "content": json.dumps({"validation_error": str(error)}, ensure_ascii=False)},
                ])
                continue

            result["symptom_review"] = review.model_dump(mode="json")
            has_failure = any(item["status"] == "unsatisfied" for item in result["comparisons"])
            failed_ids = {item["condition_id"] for item in result["comparisons"]
                          if item["status"] == "unsatisfied"}
            cited_ids = {item.condition_id for item in review.evidence}
            if review.match == "matched" and failed_ids.intersection(cited_ids):
                return finish(ReproductionStatus.REPRODUCED, "정상 기대 조건의 불충족과 제보 현상의 일치를 확인했습니다.")
            if review.match == "not_matched" and not has_failure:
                return finish(ReproductionStatus.NOT_REPRODUCED, "이번 실행에서는 기대 조건을 충족했고 제보 현상을 확인하지 못했습니다.")
            return finish(ReproductionStatus.INCONCLUSIVE, "조건 비교와 현상 대조가 충돌하거나 확인이 부족합니다.")

        return result
