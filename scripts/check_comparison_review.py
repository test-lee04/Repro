"""표시 문구의 포함 요구와 전체 일치 요구를 원문에 따라 검토하는지 검사한다."""

import copy
from contextlib import redirect_stdout
from io import StringIO
import json
import unittest

from app.agent.report_schema import BugSpec
from app.agent.verification_planner import VerificationPlanner


class Client:
    """정해진 응답으로 재생성과 사용량 집계를 확인한다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def generate(self, messages, schema):
        """모델 대신 응답을 반환하고 호출 입력을 보관한다."""
        self.requests.append(copy.deepcopy(messages))
        return {"content": json.dumps(self.responses.pop(0), ensure_ascii=False),
                "usage": {"backend": "fixture", "model": "fixture", "input_tokens": 10, "output_tokens": 5}}


def condition(report, operator="contains", value="준비 완료."):
    """평가 사이트와 무관한 표시 문구 조건을 만든다."""
    return {"status": "candidate", "summary": "처리 결과 확인",
            "conditions": [{"condition_id": "result", "kind": "text",
                            "target_description": "처리 결과 영역", "operator": operator,
                            "expected_value": value, "source": "report", "evidence": report,
                            "checkpoint": "처리 버튼을 누른 후"}],
            "questions": [], "limitation": None}


def review(report, status="consistent", evidence_id="evidence-1"):
    """대상 범위와 비교 의미를 분리한 검토 응답을 만든다."""
    return {"results": [{"condition_id": "result", "status": "same_scope", "evidence_id": "evidence-1",
                         "reason": "처리 결과 영역을 유지합니다.", "suggested_target": None,
                         "comparison": {"status": status, "evidence_id": evidence_id,
                                        "reason": "원문의 비교 강도와 기대 문구를 대조했습니다."}}]}


def spec(report):
    """원문 근거가 확정된 제보를 준비한다."""
    return BugSpec(summary="처리 결과 문구 문제", reported_steps=["처리 버튼을 누릅니다."], inputs=[],
                   actual_behavior="처리 결과가 비어 있습니다.", expected_behavior=report,
                   expectation_source="report", expectation_evidence=report, missing_information=[])


class ComparisonReviewTests(unittest.IsolatedAsyncioTestCase):
    """잘못된 비교를 직접 보정하지 않고 원문에 따라 다시 생성한다."""

    async def test_inclusion_replanned(self):
        """포함 요구를 전체 일치로 만든 응답을 거부하고 재생성한다."""
        report = "처리 결과 영역에 준비 완료.라는 문구가 표시돼야 합니다."
        client = Client([condition(report, "equals", "준비 완료"), review(report, "mismatch"),
                         condition(report), review(report)])
        result = await VerificationPlanner(client).plan(spec(report), report=report)
        self.assertEqual(result["plan"].conditions[0].operator, "contains")
        self.assertEqual(result["plan"].conditions[0].expected_value, "준비 완료.")
        self.assertEqual(result["usage"]["model_calls"], 4)
        self.assertEqual(result["usage"]["input_tokens"], 40)
        self.assertIsNotNone(result["attempts"][0]["validation_error"])
        feedback = json.loads(client.requests[2][-1]["content"])
        self.assertIn("comparison_status", feedback["validation_error"])

    async def test_exact_match_preserved(self):
        """전체 문구의 정확한 일치 요구는 equals를 유지한다."""
        report = "처리 결과 영역의 전체 문자열은 공백과 마침표까지 정확히 준비 완료.여야 합니다."
        client = Client([condition(report, "equals"), review(report)])
        result = await VerificationPlanner(client).plan(spec(report), report=report)
        self.assertEqual(result["plan"].conditions[0].operator, "equals")

    async def test_weakened_exact_rejected(self):
        """정확한 일치 요구를 약화한 조건은 실행하지 않고 확인을 요청한다."""
        report = "처리 결과 영역의 전체 문자열은 정확히 준비 완료.여야 합니다."
        client = Client([condition(report), review(report, "mismatch")])
        result = await VerificationPlanner(client, max_attempts=1).plan(spec(report), report=report)
        self.assertEqual(result["plan"].status, "needs_information")
        self.assertEqual(result["plan"].conditions, [])
        self.assertEqual(len(client.requests), 2)

    async def test_unclear_not_accepted(self):
        """비교 의미가 불명확한 응답을 정상 후보로 승인하지 않는다."""
        report = "처리 결과에 준비 완료.라는 문구가 표시돼야 합니다."
        client = Client([condition(report), review(report, "unclear")])
        result = await VerificationPlanner(client, max_attempts=1).plan(spec(report), report=report)
        self.assertEqual(result["plan"].status, "needs_information")
        self.assertTrue(result["plan"].questions)

    async def test_invented_reference_rejected(self):
        """비교 검토의 근거도 배정한 식별자를 사용해야 한다."""
        report = "처리 결과에 준비 완료.라는 문구가 표시돼야 합니다."
        client = Client([condition(report), review(report, evidence_id="invented")])
        with self.assertRaises(ValueError):
            await VerificationPlanner(client, max_attempts=1, max_review_attempts=1).plan(spec(report), report=report)

    async def test_missing_comparison_rejected(self):
        """비교 검토가 누락된 응답을 통과시키지 않는다."""
        report = "처리 결과에 준비 완료.라는 문구가 표시돼야 합니다."
        output = review(report)
        del output["results"][0]["comparison"]
        with self.assertRaises(ValueError):
            await VerificationPlanner(Client([condition(report), output]), max_attempts=1, max_review_attempts=1).plan(spec(report), report=report)

    async def test_no_actual_value_in_review(self):
        """실제 화면과 정답 없이 원문과 생성 조건만 검토한다."""
        report = "처리 결과에 준비 완료.라는 문구가 표시돼야 합니다."
        client = Client([condition(report), review(report)])
        await VerificationPlanner(client).plan(spec(report), report=report)
        payload = json.loads(client.requests[1][1]["content"])
        item = payload["conditions"][0]
        self.assertEqual(item["expected_value"], "준비 완료.")
        self.assertEqual(item["operator"], "contains")
        self.assertNotIn("actual_value", item)
        self.assertNotIn("observation", payload)

    async def test_same_call_reviews_both(self):
        """기존 범위 검토 호출 안에서 비교 의미도 검사한다."""
        report = "처리 결과에 준비 완료.라는 문구가 표시돼야 합니다."
        client = Client([condition(report), review(report)])
        result = await VerificationPlanner(client).plan(spec(report), report=report)
        self.assertEqual(result["usage"]["model_calls"], 2)
        self.assertEqual(result["attempts"][0]["scope_review"]["review"]["results"][0]["comparison"]["status"], "consistent")


if __name__ == "__main__":
    # 일부러 만든 오류의 출력은 검사 결과와 혼동하지 않도록 모은다.
    # 실제 평가 실행의 오류 출력과 진단 기록은 그대로 유지한다.
    with redirect_stdout(StringIO()):
        unittest.main(verbosity=2)
