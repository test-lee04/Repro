"""기대 문구 확인 답변이 초기 조건과 행동을 다시 생성하지 않는지 검사한다."""

from contextlib import redirect_stdout
from io import StringIO
import copy
import json
import time
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest

from app.agent.report_parser import ReportParser, expectation_context
from app.agent.report_schema import BugSpec, ClarificationAnswer

REPORT = '이름 입력란은 빈 상태입니다. 확인 버튼을 클릭하면 완료가 나옵니다. 처리 결과에 "이름을 입력해 주세요."가 포함돼야 합니다.'
EVIDENCE = '처리 결과에 "이름을 입력해 주세요."가 포함돼야 합니다.'


def data():
    """초기 상태와 실제 클릭 하나가 있는 해석을 만든다."""
    return {"summary": "빈 이름 처리 문제",
            "preconditions": [{"description": "이름 입력란이 비어 있습니다.", "source": "report", "evidence": "이름 입력란은 빈 상태입니다."}],
            "reported_steps": ["확인 버튼을 클릭합니다."], "inputs": [], "actual_behavior": "완료가 나옵니다.",
            "expected_behavior": "이름 입력 안내 문구가 포함돼야 합니다.",
            "expectation_source": "report", "expectation_evidence": EVIDENCE, "missing_information": []}


def answer():
    """기대 문구와 비교 범위만 확인한 사용자 답변을 준비한다."""
    return ClarificationAnswer(question="정확한 문구와 비교 범위는 무엇인가요?",
                              answer='"이름을 입력해 주세요."라는 문구가 포함되면 됩니다.')


class Client:
    """해석 재생성이 있는 경우에만 준비한 응답을 반환한다."""

    def __init__(self, outputs=()):
        self.outputs = list(outputs)
        self.calls = 0

    async def generate(self, messages, schema):
        """호출 횟수와 응답을 기록한다."""
        self.calls += 1
        return {"content": json.dumps(self.outputs.pop(0), ensure_ascii=False),
                "usage": {"backend": "fixture", "model": "fixture", "input_tokens": 10, "output_tokens": 5}}


class RetainedInterpretationTests(unittest.IsolatedAsyncioTestCase):
    """재현 정보 보존과 잘못된 문맥 재사용 방지를 확인한다."""

    async def test_expectation_answer_keeps_actions(self):
        """문구 답변만 있으면 초기 조건과 클릭 하나를 그대로 유지한다."""
        original = BugSpec.model_validate(data())
        context = expectation_context(original, REPORT, "", [])
        client = Client()
        result = await ReportParser(client, retained_context=context).parse(REPORT, answers=[answer()])
        self.assertEqual(result["spec"], original)
        self.assertEqual(client.calls, 0)
        self.assertEqual(result["usage"]["model_calls"], 0)
        self.assertEqual(result["interpretation_mode"], "retained_for_expectation")
        self.assertEqual(result["answers"][0]["answer"], answer().answer)

    async def test_context_copy_is_independent(self):
        """보존 문맥을 나중에 수정해도 준비한 해석기가 바뀌지 않는다."""
        context = expectation_context(BugSpec.model_validate(data()), REPORT, "", [])
        parser = ReportParser(Client(), retained_context=context)
        context["spec"]["reported_steps"].append("새로고침합니다.")
        result = await parser.parse(REPORT, answers=[answer()])
        self.assertEqual(result["spec"].reported_steps, ["확인 버튼을 클릭합니다."])

    async def test_changed_report_rejected(self):
        """다른 원래 제보에 기존 행동을 잘못 재사용하지 않는다."""
        context = expectation_context(BugSpec.model_validate(data()), REPORT, "", [])
        with self.assertRaises(ValueError):
            await ReportParser(Client(), retained_context=context).parse(REPORT + " 다른 제보입니다.", answers=[answer()])

    async def test_changed_spec_rejected(self):
        """공개 요구사항이 바뀌면 전체 해석이 필요하다."""
        context = expectation_context(BugSpec.model_validate(data()), REPORT, "", [])
        with self.assertRaises(ValueError):
            await ReportParser(Client(), retained_context=context).parse(REPORT, public_spec="새 요구사항", answers=[answer()])

    async def test_no_new_answer_rejected(self):
        """새 확인 답변이 없는 상태에서 재사용 경로를 호출하지 못한다."""
        context = expectation_context(BugSpec.model_validate(data()), REPORT, "", [])
        with self.assertRaises(ValueError):
            await ReportParser(Client(), retained_context=context).parse(REPORT)

    async def test_changed_answer_history_rejected(self):
        """기존 답변을 바꾸면서 이전 해석을 재사용하지 못한다."""
        context = expectation_context(BugSpec.model_validate(data()), REPORT, "", [answer()])
        other = ClarificationAnswer(question="다른 질문", answer="다른 답변")
        with self.assertRaises(ValueError):
            await ReportParser(Client(), retained_context=context).parse(REPORT, answers=[other, answer()])

    async def test_unresolved_base_rejected(self):
        """초기 해석의 질문이 남았으면 기존 해석 재사용으로 우회하지 못한다."""
        source = data(); source["missing_information"] = [{"field": "inputs", "question": "입력값은 무엇인가요?"}]
        context = expectation_context(BugSpec.model_validate(source), REPORT, "", [])
        with self.assertRaises(ValueError):
            await ReportParser(Client(), retained_context=context).parse(REPORT, answers=[answer()])

    async def test_full_reparse_allows_action_correction(self):
        """전체 재해석 경로에서는 사용자가 정정한 실제 조작을 반영할 수 있다."""
        updated = data(); updated["reported_steps"] = ["확인 버튼을 클릭합니다.", "브라우저를 새로고침합니다."]
        correction = ClarificationAnswer(question="행동 순서도 정정하시나요?", answer="확인 버튼을 누른 뒤 새로고침했습니다.")
        client = Client([updated])
        result = await ReportParser(client).parse(REPORT, answers=[correction])
        self.assertEqual(len(result["spec"].reported_steps), 2)
        self.assertEqual(client.calls, 1)


class RetainedAnswerApiTests(unittest.TestCase):
    """서버가 만든 기대 조건 질문만 해석 보존 경로에 연결한다."""

    def setUp(self):
        """실제 모델이나 외부 사이트 없이 답변 API와 해석기를 연결한다."""
        from fastapi.testclient import TestClient
        from app.api.run_service import RunService
        from app.main import create_app
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.received = []
        async def worker(request, run_id, folder, *, answers=None):
            if not answers:
                return {"workflow_status": "needs_information", "reproduction_status": "needs_information",
                        "questions": ["정확한 기대 문구와 비교 범위를 알려 주세요."],
                        "interpretation": {"spec": data()},
                        "condition_generation": {"clarification_scope": "expectation"}, "error": None}
            path = folder / "interpretation_context.json"
            context = json.loads(path.read_text()) if path.exists() else None
            corrected = data(); corrected["reported_steps"].append("브라우저를 새로고침합니다.")
            client = Client([corrected])
            result = await ReportParser(client, retained_context=context).parse(request.report, answers=answers)
            self.received.append({"mode": result.get("interpretation_mode"), "calls": client.calls,
                                  "spec": result["spec"].model_dump(mode="json")})
            return {"workflow_status": "conditions_checked", "reproduction_status": "not_reproduced",
                    "questions": [], "interpretation": {"spec": result["spec"].model_dump(mode="json")}, "error": None}
        self.worker = worker
        self.service = RunService(self.root, worker)
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()

    def tearDown(self):
        """검사 서버와 임시 기록을 정리한다."""
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def wait(self, run_id):
        """백그라운드 작업이 응답을 저장할 때까지 기다린다."""
        for _ in range(200):
            result = self.client.get(f"/api/runs/{run_id}").json()
            if result["status"] in {"completed", "needs_information", "failed"}:
                return result
            time.sleep(0.005)
        self.fail("작업이 종료되지 않았습니다.")

    def submit(self):
        """기대 조건 질문이 있는 원래 실행을 접수한다."""
        result = self.client.post("/api/runs", json={"report": REPORT, "target": {"start_url": "http://example.test/"}})
        self.assertEqual(result.status_code, 202)
        return self.wait(result.json()["run_id"])

    def payload(self, parent, flag=False):
        """서버가 발급한 질문 식별자와 답변 범위를 전달한다."""
        return {"answers": {item["question_id"]: answer().answer for item in parent["questions"]},
                "reparse_report": flag}

    def test_api_preserves_context(self):
        """기대 문구 답변은 기존 행동을 유지하고 문맥을 디스크에 남긴다."""
        parent = self.submit()
        response = self.client.post(f"/api/runs/{parent['run_id']}/answers", json=self.payload(parent))
        self.assertEqual(response.status_code, 202)
        child = self.wait(response.json()["run_id"])
        self.assertEqual(child["status"], "completed")
        self.assertEqual(self.received[-1]["calls"], 0)
        self.assertEqual(self.received[-1]["spec"]["reported_steps"], data()["reported_steps"])
        self.assertTrue((self.service.folder(child["run_id"]) / "interpretation_context.json").exists())

    def test_explicit_full_reparse(self):
        """사용자가 행동도 정정한다고 선택하면 전체 해석을 허용한다."""
        parent = self.submit()
        response = self.client.post(f"/api/runs/{parent['run_id']}/answers", json=self.payload(parent, True))
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.wait(response.json()["run_id"])["status"], "completed")
        self.assertEqual(self.received[-1]["calls"], 1)
        self.assertEqual(len(self.received[-1]["spec"]["reported_steps"]), 2)

    def test_duplicate_scope_change_rejected(self):
        """같은 답변을 다른 해석 범위로 바꿔 중복 실행하지 않는다."""
        parent = self.submit()
        url = f"/api/runs/{parent['run_id']}/answers"
        self.assertEqual(self.client.post(url, json=self.payload(parent)).status_code, 202)
        self.assertEqual(self.client.post(url, json=self.payload(parent, True)).status_code, 409)

    def test_non_boolean_scope_rejected(self):
        """문자열 값을 불리언으로 암묵 변환하지 않는다."""
        parent = self.submit()
        payload = self.payload(parent); payload["reparse_report"] = "false"
        self.assertEqual(self.client.post(f"/api/runs/{parent['run_id']}/answers", json=payload).status_code, 422)

    def test_restore_parent_preserves_mode(self):
        """서버 재시작 후에도 이전 해석을 읽어 기대 조건 답변에 재사용한다."""
        from fastapi.testclient import TestClient
        from app.api.run_service import RunService
        from app.main import create_app
        parent = self.submit()
        self.client.__exit__(None, None, None)
        self.service = RunService(self.root, self.worker)
        self.client = TestClient(create_app(self.service)); self.client.__enter__()
        response = self.client.post(f"/api/runs/{parent['run_id']}/answers", json=self.payload(parent))
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.wait(response.json()["run_id"])["status"], "completed")
        self.assertEqual(self.received[-1]["mode"], "retained_for_expectation")

    def test_general_questions_do_not_freeze(self):
        """일반 정보 부족 질문에는 기대 조건 전용 보존을 적용하지 않는다."""
        parent = self.submit()
        path = self.service.folder(parent["run_id"]) / "workflow_result.json"
        result = json.loads(path.read_text()); result["condition_generation"] = {}
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        response = self.client.post(f"/api/runs/{parent['run_id']}/answers", json=self.payload(parent))
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.wait(response.json()["run_id"])["status"], "completed")
        self.assertEqual(self.received[-1]["calls"], 1)


if __name__ == "__main__":
    with redirect_stdout(StringIO()):
        unittest.main(verbosity=2)
