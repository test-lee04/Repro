"""실패한 검토 단계의 재시도, 진단 기록과 제보 생성 형식의 모순 방지를 검사한다."""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.agent.report_parser import ReportParser
from app.agent.verification_planner import VerificationPlanner, VerificationPlanningError
from scripts.check_scope_review import Client, REPORT, plan, review, spec


class StageRetryTests(unittest.IsolatedAsyncioTestCase):
    """형식 오류와 의미 불일치를 서로 다른 재시도로 처리한다."""

    async def test_review_error_retries_review_only(self):
        """근거 참조 오류는 조건을 다시 생성하지 않고 검토 응답만 고친다."""
        client = Client([plan(), review(evidence_id="invented"), review()])
        result = await VerificationPlanner(client).plan(spec(), report=REPORT)
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["usage"]["model_calls"], 3)
        self.assertEqual(result["usage"]["input_tokens"], 30)
        self.assertEqual(client.payloads[1], client.payloads[2])
        self.assertIn("validation_error", json.loads(client.messages[2][-1]["content"]))
        self.assertEqual(len(result["attempts"][0]["scope_review"]["attempts"]), 2)

    async def test_review_failure_does_not_regenerate(self):
        """검토 재시도가 소진되면 조건 생성기로 되돌리지 않는다."""
        client = Client([plan(), review(evidence_id="invented"), review(evidence_id="invented")])
        with self.assertRaises(VerificationPlanningError) as caught:
            await VerificationPlanner(client).plan(spec(), report=REPORT)
        self.assertEqual(len(client.payloads), 3)
        self.assertEqual(len(caught.exception.attempts), 1)
        saved = json.loads(Path(caught.exception.diagnostic_path).read_text())
        attempts = saved["attempts"][0]["scope_review"]["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertIn("invented", attempts[0]["content"])
        self.assertTrue(attempts[1]["validation_error"])

    async def test_semantic_mismatch_regenerates_condition(self):
        """유효한 검토가 조건 불일치를 지적하면 조건을 다시 생성한다."""
        client = Client([plan("알림 항목"), review("mismatch"), plan(), review()])
        result = await VerificationPlanner(client).plan(spec(), report=REPORT)
        self.assertEqual(len(result["attempts"]), 2)
        self.assertIn("bug_spec", client.payloads[2])
        self.assertEqual(result["usage"]["model_calls"], 4)

    async def test_cross_condition_reference_rejected(self):
        """존재하는 근거라도 다른 조건에 배정된 근거면 거부한다."""
        output = plan(); other = dict(output["conditions"][0]); other["condition_id"] = "other"
        output["conditions"].append(other)
        response = review(); other_result = json.loads(json.dumps(response["results"][0]))
        other_result["condition_id"] = "other"
        response["results"].append(other_result)
        with self.assertRaises(VerificationPlanningError):
            await VerificationPlanner(Client([output, response]), max_review_attempts=1).plan(spec(), report=REPORT)

    async def test_original_evidence_preserved(self):
        """근거 원문을 재작성하지 않고 참조 목록에 보존한다."""
        result = await VerificationPlanner(Client([plan(), review()])).plan(spec(), report=REPORT)
        record = result["attempts"][0]["scope_review"]
        self.assertEqual(record["evidence_catalog"]["evidence-1"]["evidence"], plan()["conditions"][0]["evidence"])
        self.assertNotIn("quote", record["review"]["results"][0])
        self.assertTrue(Path(result["diagnostic_path"]).is_file())

    async def test_old_quote_response_rejected(self):
        """이전 응답 형식으로 근거 문장을 재작성하면 승인하지 않는다."""
        response = review(); response["results"][0]["quote"] = "임의의 인용문"
        client = Client([plan(), response, review()])
        result = await VerificationPlanner(client).plan(spec(), report=REPORT)
        self.assertEqual(result["usage"]["model_calls"], 3)

    def test_known_expectation_schema_excludes_same_question(self):
        """확정된 기대 동작 분기는 같은 기대 동작 질문을 생성할 수 없다."""
        schema = ReportParser.generation_schema()
        known = schema["anyOf"][0]["properties"]
        self.assertNotIn("unknown", known["expectation_source"]["enum"])
        self.assertNotIn("expected_behavior", known["missing_information"]["items"]["properties"]["field"]["enum"])
        self.assertEqual(known["expected_behavior"]["type"], "string")
        self.assertIn("inputs", known["missing_information"]["items"]["properties"]["field"]["enum"])

    def test_unknown_expectation_keeps_question(self):
        """불명확한 기대 동작 분기는 null과 기대 동작 질문을 유지한다."""
        unknown = ReportParser.generation_schema()["anyOf"][1]["properties"]
        self.assertEqual(unknown["expectation_source"]["enum"], ["unknown"])
        self.assertEqual(unknown["expected_behavior"]["type"], "null")
        self.assertIn("expected_behavior", unknown["missing_information"]["items"]["properties"]["field"]["enum"])


if __name__ == "__main__":
    # 일부러 만든 오류의 출력은 검사 결과와 혼동하지 않도록 모은다.
    # 실제 평가 실행의 오류 출력과 진단 기록은 그대로 유지한다.
    with redirect_stdout(StringIO()):
        unittest.main(verbosity=2)
