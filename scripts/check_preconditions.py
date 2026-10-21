"""초기 상태의 근거와 실제 행동 목록의 분리 및 기존 해석 형식 호환성을 검사한다."""

from contextlib import redirect_stdout
from io import StringIO
import json
import unittest

from pydantic import ValidationError

from app.agent.checkpoint_reviewer import CheckpointReviewer
from app.agent.report_parser import ReportParser
from app.agent.report_schema import BugSpec, ClarificationAnswer

REPORT = '이름 입력란은 빈 상태입니다. 확인 버튼을 클릭하면 완료라고 나옵니다. 처리 결과에는 "이름을 입력해 주세요."라는 문구가 포함돼야 합니다.'
EVIDENCE = '처리 결과에는 "이름을 입력해 주세요."라는 문구가 포함돼야 합니다.'


def data():
    """초기 상태와 버튼 조작을 구분한 해석을 만든다."""
    return {"summary": "빈 이름 처리 문제",
            "preconditions": [{"description": "이름 입력란이 비어 있습니다.", "source": "report", "evidence": "이름 입력란은 빈 상태입니다."}],
            "reported_steps": ["확인 버튼을 클릭합니다."], "inputs": [], "actual_behavior": "완료라고 나옵니다.",
            "expected_behavior": "이름 입력 안내 문구가 포함돼야 합니다.", "expectation_source": "report",
            "expectation_evidence": EVIDENCE, "missing_information": []}


class Client:
    """준비한 응답과 요청 기록으로 연결을 검사한다."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.payloads = []

    async def generate(self, messages, schema):
        """모델 호출 없이 JSON 응답을 반환한다."""
        self.payloads.append(json.loads(messages[1]["content"]))
        return {"content": json.dumps(self.outputs.pop(0), ensure_ascii=False),
                "usage": {"backend": "fixture", "model": "fixture", "input_tokens": 10, "output_tokens": 5}}


class PreconditionTests(unittest.IsolatedAsyncioTestCase):
    """원문 근거는 검증하되 실제 초기 상태를 확인했다고 주장하지 않는다."""

    async def test_parse_separate_state_and_action(self):
        """초기 빈 상태를 별도 행동으로 추가하지 않은 해석을 연결한다."""
        result = await ReportParser(Client([data()])).parse(REPORT)
        self.assertEqual(len(result["spec"].preconditions), 1)
        self.assertEqual(result["spec"].reported_steps, ["확인 버튼을 클릭합니다."])
        self.assertEqual(result["spec"].inputs, [])

    async def test_old_response_compatible(self):
        """초기 상태 필드가 없는 이전 응답도 읽는다."""
        old = data(); del old["preconditions"]
        self.assertEqual(BugSpec.model_validate(old).preconditions, [])

    async def test_public_spec_cannot_assert_initial_state(self):
        """요구사항을 실제 초기 상태의 출처로 사용하지 못한다."""
        output = data(); output["preconditions"][0]["source"] = "public_spec"
        with self.assertRaises(ValidationError):
            BugSpec.model_validate(output)

    async def test_invented_state_evidence_rejected(self):
        """원문에 없는 초기 상태 근거를 거부한다."""
        output = data(); output["preconditions"][0]["evidence"] = "이미 인증된 상태입니다."
        with self.assertRaises(ValueError):
            await ReportParser(Client([output]), max_attempts=1).parse(REPORT)

    async def test_user_answer_evidence_allowed(self):
        """추가 답변으로 명시된 초기 상태를 허용한다."""
        output = data(); output["preconditions"][0].update(source="user_answer", evidence="처음부터 빈 상태였습니다.")
        result = await ReportParser(Client([output])).parse(REPORT,
            answers=[ClarificationAnswer(question="초기 상태는 무엇인가요?", answer="처음부터 빈 상태였습니다.")])
        self.assertEqual(result["spec"].preconditions[0].source, "user_answer")

    async def test_system_question_not_state_evidence(self):
        """시스템 질문만으로 초기 상태를 확정하지 못한다."""
        output = data(); output["preconditions"][0].update(source="user_answer", evidence="입력란이 빈 상태였나요?")
        with self.assertRaises(ValueError):
            await ReportParser(Client([output]), max_attempts=1).parse(REPORT,
                answers=[ClarificationAnswer(question="입력란이 빈 상태였나요?", answer="기억나지 않습니다.")])

    async def test_explicit_clearing_is_action(self):
        """사용자가 직접 비운 조작은 행동 목록에 유지할 수 있다."""
        output = data(); output["preconditions"] = []
        output["reported_steps"] = ["이름 입력란의 값을 모두 지웁니다.", "확인 버튼을 클릭합니다."]
        report = "이름 입력란의 값을 모두 지운 뒤 확인 버튼을 클릭했습니다. " + EVIDENCE
        result = await ReportParser(Client([output])).parse(report)
        self.assertEqual(len(result["spec"].reported_steps), 2)
        self.assertEqual(result["spec"].preconditions, [])

    async def test_generation_schema_exposes_state(self):
        """모델의 생성 형식에 초기 상태 필드를 명시한다."""
        schema = ReportParser.generation_schema()
        for branch in schema["anyOf"]:
            self.assertIn("preconditions", branch["required"])
            self.assertIn("preconditions", branch["properties"])

    async def test_checkpoint_requires_only_actions(self):
        """상태를 가짜 행동 번호로 채우지 않고 실제 클릭 하나를 확인한다."""
        output = {"status": "reached", "coverage": [
            {"step_number": 1, "action_numbers": [1], "reason": "확인 버튼을 클릭했습니다."}],
            "checkpoint_action_numbers": [1], "reason": "제보의 조작 하나를 수행했습니다."}
        client = Client([output])
        result = await CheckpointReviewer(client).review_completion(BugSpec.model_validate(data()), [
            {"action": {"kind": "click", "element_id": "button"}, "success": True,
             "before_elements": [{"element_id": "button", "tag": "button", "name": "확인"}]}])
        self.assertEqual(result["review"].status, "reached")
        self.assertEqual(len(client.payloads[0]["reported_steps"]), 1)
        self.assertEqual(client.payloads[0]["actions"][0]["kind"], "click")


if __name__ == "__main__":
    with redirect_stdout(StringIO()):
        unittest.main(verbosity=2)
