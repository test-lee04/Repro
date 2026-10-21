"""검증 조건 생성 결과의 형식과 상태 규칙을 검사한다."""

import unittest

from pydantic import ValidationError

from app.agent.verification_schema import VerificationPlan


def make_condition(
    condition_id: str = "saved_title",
) -> dict:
    """검사에 사용할 조건 데이터를 만든다.

    Args:
        condition_id: 조건 식별자.

    Returns:
        저장된 제목 값을 확인하는 검사 데이터.

    Notes:
        이 데이터는 형식 검사용 예시다.
        실제 서비스의 조건 생성에 전달하지 않는다.
    """
    return {
        "condition_id": condition_id,
        "kind": "value",
        "target_description": "메모 제목 입력창",
        "operator": "equals",
        "expected_value": "주간 계획",
        "source": "report",
        "evidence": (
            "저장한 제목 주간 계획이 "
            "새로고침 후에도 유지돼야 합니다."
        ),
        "checkpoint": "저장 후 새로고침한 시점",
    }


class VerificationSchemaTests(unittest.TestCase):
    """조건 생성 결과의 공통 규칙을 확인한다."""

    def test_candidate(self):
        """조건 후보를 기존 공통 조건 모델로 표현한다."""
        plan = VerificationPlan.model_validate(
            {
                "status": "candidate",
                "summary": (
                    "새로고침 후 저장한 제목을 확인합니다."
                ),
                "conditions": [make_condition()],
                "questions": [],
                "limitation": None,
            }
        )

        self.assertEqual(len(plan.conditions), 1)
        self.assertEqual(
            plan.conditions[0].expected_value,
            "주간 계획",
        )

    def test_needs_information(self):
        """기대 결과가 부족하면 질문으로 표현한다."""
        plan = VerificationPlan.model_validate(
            {
                "status": "needs_information",
                "summary": "정상 결과를 확인해야 합니다.",
                "conditions": [],
                "questions": [
                    "정상적으로 표시돼야 하는 결과는 무엇인가요?"
                ],
                "limitation": None,
            }
        )

        self.assertEqual(
            plan.status,
            "needs_information",
        )

    def test_unsupported(self):
        """현재 방식으로 검사할 수 없는 이유를 남긴다."""
        plan = VerificationPlan.model_validate(
            {
                "status": "unsupported",
                "summary": (
                    "현재 화면 관찰만으로 확인하기 어렵습니다."
                ),
                "conditions": [],
                "questions": [],
                "limitation": (
                    "오디오 재생 품질의 비교는 "
                    "현재 검증 조건에서 지원하지 않습니다."
                ),
            }
        )

        self.assertEqual(plan.status, "unsupported")

    def test_empty_candidate_rejected(self):
        """조건이 없는 후보 상태를 거부한다."""
        with self.assertRaises(ValidationError):
            VerificationPlan.model_validate(
                {
                    "status": "candidate",
                    "summary": "조건을 생성했습니다.",
                    "conditions": [],
                }
            )

    def test_missing_question_rejected(self):
        """추가 정보 필요 상태에 질문이 없으면 거부한다."""
        with self.assertRaises(ValidationError):
            VerificationPlan.model_validate(
                {
                    "status": "needs_information",
                    "summary": "정보가 부족합니다.",
                    "questions": [],
                }
            )

    def test_duplicate_condition_ids_rejected(self):
        """조건 식별자가 중복되면 거부한다."""
        with self.assertRaises(ValidationError):
            VerificationPlan.model_validate(
                {
                    "status": "candidate",
                    "summary": "제목을 확인합니다.",
                    "conditions": [
                        make_condition(),
                        make_condition(),
                    ],
                }
            )

    def test_conditions_with_questions_rejected(self):
        """미확인 조건이 실행 후보로 전달되는 것을 막는다."""
        with self.assertRaises(ValidationError):
            VerificationPlan.model_validate(
                {
                    "status": "needs_information",
                    "summary": "추가 확인이 필요합니다.",
                    "conditions": [make_condition()],
                    "questions": [
                        "정상 제목은 무엇인가요?"
                    ],
                }
            )

    def test_unknown_source_rejected(self):
        """기존 공통 조건의 출처 검사 규칙을 유지한다."""
        condition = make_condition()
        condition["source"] = "unknown"

        with self.assertRaises(ValidationError):
            VerificationPlan.model_validate(
                {
                    "status": "candidate",
                    "summary": "제목을 확인합니다.",
                    "conditions": [condition],
                }
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)