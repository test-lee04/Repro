"""후보 질문을 보존하고 관찰에 진입하되 행동 단계의 필수 정보 확인은 유지한다."""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import unittest

from app.agent.action_planner import ActionPlanner
from app.agent.report_parser import ReportParser
from app.agent.question_reviewer import ObservationGate
from app.agent.report_schema import BugSpec

REPORT = "입력란이 비어 있는 상태에서 확인 버튼을 누르면 완료라고 나옵니다. 비어 있으면 입력을 요청하는 안내가 표시돼야 합니다."


def parsed(questions):
    """기대 동작이 알려진 해석과 후보 질문을 준비한다."""
    return {"summary": "빈 입력 처리 문제", "reported_steps": ["입력란이 비어 있는 상태에서 확인 버튼을 누릅니다."],
            "inputs": [], "actual_behavior": "완료라고 나옵니다.", "expected_behavior": "입력을 요청하는 안내가 표시돼야 합니다.",
            "expectation_source": "report", "expectation_evidence": REPORT, "missing_information": questions}


class Client:
    """준비한 응답을 반환하고 실제 모델 호출 수를 기록한다."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0
        self.schemas = []

    async def generate(self, messages, schema):
        """JSON 응답 하나를 반환한다."""
        self.calls += 1
        self.schemas.append(schema)
        return {"content": json.dumps(self.outputs.pop(0), ensure_ascii=False),
                "usage": {"backend": "fixture", "model": "fixture", "input_tokens": 10, "output_tokens": 5}}


class QuestionReviewTests(unittest.IsolatedAsyncioTestCase):
    """질문을 해결했다고 주장하지 않고 관찰 이후로 이월한다."""

    async def test_defer_without_extra_model(self):
        """조작과 기대 동작이 알려지면 후보 질문 때문에 화면 관찰을 막지 않는다."""
        questions = [{"field": "actual_behavior", "question": "버튼을 다시 누르면 어떤 결과가 나오나요?"},
                     {"field": "other", "question": "문구의 정확한 위치가 어디인가요?"}]
        client = Client([parsed(questions)])
        result = await ReportParser(client).parse(REPORT)
        self.assertFalse(result["spec"].needs_clarification)
        self.assertEqual(result["question_review"]["deferred_questions"], questions)
        self.assertEqual(result["question_review"]["excluded_questions"], [])
        self.assertEqual(client.calls, 1)
        self.assertEqual(result["usage"]["model_calls"], 1)

    async def test_unknown_expectation_still_waits(self):
        """정상 기대 동작이 없으면 답변을 기다린다."""
        data = parsed([{ "field": "expected_behavior", "question": "정상 기대 동작은 무엇인가요?"}])
        data.update(expected_behavior=None, expectation_source="unknown", expectation_evidence=None)
        result = await ReportParser(Client([data])).parse(REPORT)
        self.assertTrue(result["spec"].needs_clarification)
        self.assertEqual(result["question_review"]["readiness"], "needs_user_information")

    async def test_missing_steps_still_waits(self):
        """제보된 조작이 없으면 후보 질문을 이월하지 않는다."""
        data = parsed([{ "field": "reported_steps", "question": "어떤 조작을 했나요?"}])
        data["reported_steps"] = []
        result = await ReportParser(Client([data])).parse(REPORT)
        self.assertTrue(result["spec"].needs_clarification)
        self.assertEqual(result["question_review"]["deferred_questions"], [])

    async def test_original_questions_saved(self):
        """원래 질문과 이월 이유를 진단 파일에 보존한다."""
        questions = [{"field": "inputs", "question": "입력값은 무엇인가요?"}]
        result = await ReportParser(Client([parsed(questions)])).parse(REPORT)
        saved = json.loads(Path(result["diagnostic_path"]).read_text())
        self.assertEqual(saved["attempts"][0]["question_review"]["original_questions"], questions)
        self.assertEqual(result["spec"].inputs, [])

    async def test_missing_input_cannot_be_invented(self):
        """질문이 이월돼도 행동 모델이 원문에 없는 값을 입력할 수 없다."""
        spec = BugSpec.model_validate(parsed([]))
        client = Client([{"decision": "act", "description": "입력합니다.",
                          "action": {"kind": "fill", "element_id": "input", "value": "임의 값"}, "question": None}])
        observation = {"elements": [{"element_id": "input", "tag": "input", "name": "이름", "enabled": True, "actions": ["fill"]}]}
        with self.assertRaisesRegex(ValueError, "없는 입력값"):
            await ActionPlanner(client).next_action(spec, observation, [])

    async def test_action_can_request_information(self):
        """관찰 후 행동을 결정할 입력값이 없으면 사용자에게 질문할 수 있다."""
        spec = BugSpec.model_validate(parsed([]))
        client = Client([{"decision": "need_information", "description": "입력값을 확인합니다.",
                          "action": None, "question": "입력해야 하는 값이 무엇인가요?"}])
        result = await ActionPlanner(client).next_action(spec, {"elements": []}, [])
        self.assertEqual(result["decision"].decision, "need_information")
        self.assertIsNone(result["decision"].action)

    async def test_no_questions_has_no_extra_call(self):
        """후보 질문이 없으면 해석 호출만 사용한다."""
        client = Client([parsed([])])
        result = await ReportParser(client).parse(REPORT)
        self.assertFalse(result["spec"].needs_clarification)
        self.assertEqual(client.calls, 1)

    async def test_expected_and_steps_unchanged(self):
        """기대값과 제보 행동을 바꾸지 않고 후보 질문만 이월한다."""
        data = parsed([{ "field": "other", "question": "정확한 위치는 어디인가요?"}])
        original = BugSpec.model_validate(data)
        record = {}
        resolved = ObservationGate.apply(original, record)
        self.assertEqual(resolved.expected_behavior, original.expected_behavior)
        self.assertEqual(resolved.reported_steps, original.reported_steps)
        self.assertEqual(resolved.expectation_evidence, original.expectation_evidence)
        self.assertTrue(original.needs_clarification)

    async def test_blank_steps_do_not_bypass_questions(self):
        """공백 조작만으로 관찰 준비가 됐다고 간주하지 않는다."""
        data = parsed([{ "field": "reported_steps", "question": "조작을 알려 주세요."}])
        data["reported_steps"] = [" "]
        original = BugSpec.model_validate(data)
        resolved = ObservationGate.apply(original, {})
        self.assertTrue(resolved.needs_clarification)


if __name__ == "__main__":
    with redirect_stdout(StringIO()):
        unittest.main(verbosity=2)
