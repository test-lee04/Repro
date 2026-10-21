"""기대 동작과 조작이 알려진 제보의 후보 질문을 화면 관찰 뒤로 이월한다."""

from app.agent.report_schema import BugSpec, ExpectationSource


class ObservationGate:
    """질문의 필요성을 추측하지 않고 첫 화면 관찰에 진입할 기준을 적용한다."""

    @staticmethod
    def apply(spec, record):
        """원래 질문을 보존하고 화면 관찰 전에 답변을 기다릴지 결정한다.

        Args:
            spec: 원문 근거 검증을 통과한 제보 해석.
            record: 원래 질문과 이월 정책을 저장할 진단 기록.

        Returns:
            기대 동작이나 조작이 불명확하면 원래 제보를 반환한다.
            그 외에는 후보 질문을 기록에 이월한 실행용 제보를 반환한다.

        Notes:
            질문을 이미 답했거나 불필요하다고 판정하는 기능이 아니다.
            행동 단계에서 대상이나 입력값이 부족하면 need_information으로 중단해야 한다.
            입력값을 새로 만들거나 정상 기대 동작을 바꾸지 않는다.
            기본 브라우저에서의 첫 시도이며 다른 환경의 검증 완료를 뜻하지 않는다.
        """
        original = [item.model_dump(mode="json") for item in spec.missing_information]
        record.update({"policy": "observe_before_candidate_questions", "original_questions": original,
                       "attempts": [], "excluded_questions": [], "deferred_questions": [],
                       "readiness": "needs_user_information"})
        has_steps = bool(spec.reported_steps) and all(step.strip() for step in spec.reported_steps)
        if spec.expectation_source == ExpectationSource.UNKNOWN or not has_steps:
            record["reason"] = "기대 동작 또는 제보의 조작이 부족해 먼저 사용자 답변이 필요합니다."
            return spec
        data = spec.model_dump(mode="json")
        data["missing_information"] = []
        resolved = BugSpec.model_validate(data)
        record.update({"readiness": "ready_for_observation", "deferred_questions": original,
                       "reason": "제보의 조작과 정상 기대 동작이 알려져 있어 먼저 화면을 관찰합니다. 후보 질문은 보존하며 실제 행동에 필요한 정보가 부족하면 그때 답변을 요청합니다."})
        return resolved
