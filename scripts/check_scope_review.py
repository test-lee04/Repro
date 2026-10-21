"""근거 식별자 기반의 범위 검토와 조건 재생성을 검사한다."""

from contextlib import redirect_stdout
from io import StringIO
import json
import unittest

from app.agent.report_schema import BugSpec
from app.agent.verification_planner import VerificationPlanner

REPORT = "갱신 버튼을 눌러도 알림 목록에 문구가 없습니다. 알림 목록에는 점검이라는 문구가 포함돼야 합니다."
EVIDENCE = "알림 목록에는 점검이라는 문구가 포함돼야 합니다."


def spec():
    """근거가 있는 검사 입력을 만든다."""
    return BugSpec(summary="알림 문구 누락", reported_steps=["갱신 버튼을 누릅니다."], inputs=[],
                   actual_behavior="문구가 없습니다.", expected_behavior=EVIDENCE,
                   expectation_source="report", expectation_evidence=EVIDENCE, missing_information=[])


def plan(target="알림 목록 전체"):
    """영역 전체의 문구 포함 조건을 만든다."""
    return {"status": "candidate", "summary": "알림 문구 확인", "conditions": [
        {"condition_id": "notice", "kind": "text", "target_description": target, "operator": "contains",
         "expected_value": "점검", "source": "report", "evidence": EVIDENCE, "checkpoint": "갱신 버튼을 누른 후"}],
        "questions": [], "limitation": None}


def review(status="same_scope", **changes):
    """조건에 배정한 근거를 그대로 참조한다."""
    item = {"condition_id": "notice", "status": status, "evidence_id": "evidence-1",
            "reason": "원문은 목록 전체를 지정합니다.",
            "suggested_target": "알림 목록 전체" if status == "mismatch" else None,
            "comparison": {"status": "consistent", "evidence_id": "evidence-1", "reason": "문구 포함 요구입니다."}}
    item.update(changes)
    return {"results": [item]}


class Client:
    """준비된 응답과 모든 요청을 기록하는 모델 대역이다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []
        self.messages = []

    async def generate(self, messages, schema):
        """응답을 하나 반환하고 호출 사용량을 제공한다."""
        self.payloads.append(json.loads(messages[1]["content"]))
        self.messages.append(json.loads(json.dumps(messages)))
        return {"content": json.dumps(self.responses.pop(0), ensure_ascii=False),
                "usage": {"backend": "fixture", "model": "fixture", "input_tokens": 10, "output_tokens": 5}}


class ScopeReviewTests(unittest.IsolatedAsyncioTestCase):
    """유효한 검토 결과만 조건 재생성의 근거로 사용한다."""

    async def test_same_scope(self):
        """대상과 비교가 일치하면 그대로 사용한다."""
        result = await VerificationPlanner(Client([plan(), review()])).plan(spec(), report=REPORT)
        self.assertEqual(result["usage"]["model_calls"], 2)

    async def test_mismatch_replanned(self):
        """유효한 범위 불일치만 생성기에 돌려보낸다."""
        client = Client([plan("알림 항목"), review("mismatch"), plan(), review()])
        result = await VerificationPlanner(client).plan(spec(), report=REPORT)
        self.assertEqual(result["usage"]["model_calls"], 4)
        self.assertEqual(result["plan"].conditions[0].target_description, "알림 목록 전체")

    async def test_mismatch_not_forced(self):
        """재생성이 끝나도 제안 대상을 강제 적용하지 않는다."""
        with self.assertRaises(ValueError):
            await VerificationPlanner(Client([plan(), review("mismatch")]), max_attempts=1).plan(spec(), report=REPORT)

    async def test_unclear_rejected(self):
        """유효하지만 불명확한 검토는 일치가 아니다."""
        with self.assertRaises(ValueError):
            await VerificationPlanner(Client([plan(), review("unclear")]), max_attempts=1).plan(spec(), report=REPORT)

    async def test_unknown_condition(self):
        """없는 조건 참조는 검토 단계에서 거부한다."""
        with self.assertRaises(ValueError):
            await VerificationPlanner(Client([plan(), review(condition_id="invented")]), max_review_attempts=1).plan(spec(), report=REPORT)

    async def test_duplicate_condition(self):
        """조건을 중복 검토할 수 없다."""
        output = review(); output["results"] *= 2
        with self.assertRaises(ValueError):
            await VerificationPlanner(Client([plan(), output]), max_review_attempts=1).plan(spec(), report=REPORT)

    async def test_blank_reference(self):
        """공백인 근거 참조를 거부한다."""
        with self.assertRaises(ValueError):
            await VerificationPlanner(Client([plan(), review(evidence_id=" ")]), max_review_attempts=1).plan(spec(), report=REPORT)

    async def test_unknown_reference(self):
        """모델이 만든 근거 식별자를 거부한다."""
        with self.assertRaises(ValueError):
            await VerificationPlanner(Client([plan(), review(evidence_id="invented")]), max_review_attempts=1).plan(spec(), report=REPORT)

    async def test_expected_forwarded_without_actual(self):
        """원문과 기대 조건만 전달하고 실제 화면 값은 전달하지 않는다."""
        client = Client([plan(), review()])
        await VerificationPlanner(client).plan(spec(), report=REPORT)
        item = client.payloads[1]["conditions"][0]
        self.assertEqual(item["expected_value"], "점검")
        self.assertNotIn("actual_value", item)
        self.assertEqual(item["evidence"], EVIDENCE)

    async def test_value_uses_existing_binding_checks(self):
        """입력값 조건은 표시 영역 검토 없이 기존 연결 검사를 사용한다."""
        output = plan(); output["conditions"][0].update(kind="value", operator="equals")
        result = await VerificationPlanner(Client([output])).plan(spec(), report=REPORT)
        self.assertEqual(result["usage"]["model_calls"], 1)

    async def test_existing_clarification(self):
        """필요한 질문이 남아 있으면 모델을 호출하지 않는다."""
        data = spec().model_dump(); data["missing_information"] = [{"field": "inputs", "question": "입력값을 알려 주세요."}]
        client = Client([])
        result = await VerificationPlanner(client).plan(BugSpec(**data), report=REPORT)
        self.assertEqual(result["plan"].status, "needs_information")
        self.assertEqual(len(client.payloads), 0)

    async def test_mixed_plan_reviews_only_text(self):
        """입력값과 문구 조건이 섞여도 문구만 검토한다."""
        output = plan(); item = dict(output["conditions"][0]); item.update(condition_id="value", kind="value", operator="equals")
        output["conditions"].append(item)
        client = Client([output, review()])
        await VerificationPlanner(client).plan(spec(), report=REPORT)
        self.assertEqual(len(client.payloads[1]["conditions"]), 1)


if __name__ == "__main__":
    # 일부러 만든 오류의 출력은 검사 결과와 혼동하지 않도록 모은다.
    # 실제 평가 실행의 오류 출력과 진단 기록은 그대로 유지한다.
    with redirect_stdout(StringIO()):
        unittest.main(verbosity=2)
