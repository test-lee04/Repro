"""공통 요청·검증 조건·판정 상태의 데이터 형식을 검증한다."""

import unittest

from pydantic import ValidationError

from app.schemas import RunRequest
from app.testing.check_schema import VerificationCondition
from app.testing.verdict import (
    ReproductionStatus,
    TestStatus,
)


class CommonSchemaTests(unittest.TestCase):
    """사례별 전용 모델 없이 공통 형식을 사용할 수 있는지 검사한다."""

    def test_target_url(self):
        """제보와 대상 URL을 함께 표현한다."""
        request = RunRequest(
            report="저장한 제목이 새로고침하면 사라집니다.",
            target={
                "start_url": "http://localhost:8000/notes",
            },
        )

        self.assertIsNone(request.target_id)
        self.assertIsNotNone(request.target)

    def test_explicit_registered_target(self):
        """명시적인 기존 대상 ID도 전환 기간에 지원한다."""
        request = RunRequest(
            report="버튼을 눌러도 화면이 바뀌지 않습니다.",
            target_id="registered_test_service",
        )

        self.assertEqual(
            request.target_id,
            "registered_test_service",
        )

    def test_missing_or_ambiguous_target(self):
        """대상이 없거나 두 방식이 겹친 요청을 거부한다."""
        with self.assertRaises(ValidationError):
            RunRequest(report="오류가 발생합니다.")

        with self.assertRaises(ValidationError):
            RunRequest(
                report="오류가 발생합니다.",
                target_id="registered_test_service",
                target={"start_url": "http://localhost:8000"},
            )

    def test_different_conditions(self):
        """서로 다른 기대 결과를 같은 조건 모델로 표현한다."""
        cases = [
            {
                "condition_id": "deleted_item_absent",
                "kind": "presence",
                "target_description": "삭제한 항목",
                "operator": "equals",
                "expected_value": False,
                "source": "report",
                "evidence": "삭제하면 해당 항목이 없어져야 합니다.",
                "checkpoint": "삭제 동작이 끝난 후",
            },
            {
                "condition_id": "saved_title_retained",
                "kind": "value",
                "target_description": "제목 입력창",
                "operator": "equals",
                "expected_value": "회의 기록",
                "source": "report",
                "evidence": "저장한 제목은 회의 기록입니다.",
                "checkpoint": "저장 후 새로고침한 뒤",
            },
            {
                "condition_id": "navigation_destination",
                "kind": "url",
                "operator": "contains",
                "expected_value": "/settings",
                "source": "public_spec",
                "evidence": "설정 버튼은 설정 화면으로 이동합니다.",
                "checkpoint": "설정 버튼 클릭 후",
            },
            {
                "condition_id": "displayed_amount",
                "kind": "number",
                "target_description": "결제 합계",
                "operator": "equals",
                "expected_value": 27000,
                "source": "report",
                "evidence": "정상 합계는 27000원이어야 합니다.",
                "checkpoint": "수량 변경이 반영된 후",
            },
        ]

        for case in cases:
            with self.subTest(condition=case["condition_id"]):
                condition = VerificationCondition.model_validate(case)
                self.assertEqual(
                    condition.condition_id,
                    case["condition_id"],
                )

    def test_invalid_condition(self):
        """존재 여부에 숫자를 넣는 등 잘못된 조합을 거부한다."""
        with self.assertRaises(ValidationError):
            VerificationCondition(
                condition_id="invalid",
                kind="presence",
                target_description="대상 항목",
                operator="equals",
                expected_value=1,
                source="report",
                evidence="항목이 보여야 합니다.",
                checkpoint="화면 표시 후",
            )

    def test_unknown_expectation_source(self):
        """근거가 불명확한 기대값을 실행 조건으로 승인하지 않는다."""
        with self.assertRaises(ValidationError):
            VerificationCondition(
                condition_id="unknown",
                kind="text",
                target_description="안내 문구",
                operator="equals",
                expected_value="완료",
                source="unknown",
                evidence="확인되지 않은 내용",
                checkpoint="버튼 클릭 후",
            )

    def test_distinct_statuses(self):
        """미재현·판정 보류와 수정 검증 대기를 구분한다."""
        self.assertNotEqual(
            ReproductionStatus.NOT_REPRODUCED,
            ReproductionStatus.INCONCLUSIVE,
        )

        self.assertEqual(
            TestStatus.AWAITING_FIX_VALIDATION.value,
            "awaiting_fix_validation",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)