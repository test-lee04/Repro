"""기대 문구 보존과 해당 부분만 묻는 확인 질문 및 실제 답변 반영을 검사한다."""

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from tempfile import TemporaryDirectory
from unittest.mock import patch
from pathlib import Path
import unittest

from app.agent.report_parser import ReportParser
from app.agent.report_schema import ClarificationAnswer
from app.agent.verification_planner import VerificationPlanner
from scripts.check_comparison_review import Client, condition, review, spec
from scripts.check_verification_planner import collect_answers


class ExpectationClarificationTests(unittest.IsolatedAsyncioTestCase):
    """잘못된 조건을 통과시키지 않고 확인 질문을 통해 다시 조건을 만든다."""

    async def test_quoted_punctuation_replanned(self):
        """인용된 문구의 마침표를 뺀 기대값은 재생성한다."""
        report = '처리 결과 영역에 "준비 완료."라는 문구가 포함돼야 합니다.'
        client = Client([condition(report, value="준비 완료"), condition(report), review(report)])
        result = await VerificationPlanner(client).plan(spec(report), report=report)
        self.assertEqual(result["plan"].conditions[0].expected_value, "준비 완료.")
        self.assertEqual(result["usage"]["model_calls"], 3)
        self.assertTrue(result["attempts"][0]["validation_error"])

    async def test_quoted_internal_spaces_preserved(self):
        """문구 안의 공백을 임의로 줄이지 않는다."""
        report = '처리 결과에 "준비  완료."가 포함돼야 합니다.'
        good = condition(report, value="준비  완료.")
        result = await VerificationPlanner(Client([good, review(report)])).plan(spec(report), report=report)
        self.assertEqual(result["plan"].conditions[0].expected_value, "준비  완료.")

    async def test_exact_literal_not_weakened(self):
        """전체 문자열의 정확한 일치는 그대로 유지한다."""
        report = '처리 결과 전체 문자열은 정확히 "준비 완료."여야 합니다.'
        result = await VerificationPlanner(Client([condition(report, "equals"), review(report)])).plan(spec(report), report=report)
        self.assertEqual(result["plan"].conditions[0].operator, "equals")

    async def test_unresolved_comparison_asks_targeted_question(self):
        """재생성 한도 뒤 문구와 비교 범위만 질문한다."""
        report = "처리 결과에 준비 완료.라는 문구가 표시돼야 합니다."
        client = Client([condition(report, "equals"), review(report, "mismatch"),
                         condition(report, "equals"), review(report, "mismatch")])
        result = await VerificationPlanner(client).plan(spec(report), report=report)
        self.assertEqual(result["plan"].status, "needs_information")
        self.assertEqual(result["plan"].conditions, [])
        self.assertEqual(len(result["plan"].questions), 1)
        self.assertIn("처리 결과 영역", result["plan"].questions[0])
        self.assertIn("큰따옴표", result["plan"].questions[0])
        self.assertEqual(result["usage"]["model_calls"], 4)
        saved = json.loads(Path(result["diagnostic_path"]).read_text())
        self.assertEqual(saved["status"], "needs_information")

    async def test_actual_answer_reparsed_and_used(self):
        """확인 질문의 실제 답변을 제보 해석과 조건 생성에 함께 전달한다."""
        report = "처리 결과에 준비 완료.라는 문구가 표시돼야 합니다."
        first = await VerificationPlanner(Client([condition(report), review(report, "unclear")]), max_attempts=1).plan(spec(report), report=report)
        answer_text = '처리 결과 영역에는 "준비 완료."가 포함되면 됩니다. 전체 문자열 일치는 필요하지 않습니다.'
        answer = ClarificationAnswer(question=first["plan"].questions[0], answer=answer_text)
        parsed = spec(report).model_dump(mode="json")
        parsed.update(expectation_source="user_answer", expectation_evidence=answer_text)
        good = condition(answer_text)
        good["conditions"][0]["source"] = "user_answer"
        client = Client([parsed, good, review(answer_text)])
        interpreted = await ReportParser(client).parse(report, answers=[answer])
        result = await VerificationPlanner(client).plan(interpreted["spec"], report=report, answers=[answer])
        self.assertEqual(result["plan"].status, "candidate")
        self.assertEqual(result["plan"].conditions[0].source.value, "user_answer")
        self.assertEqual(client.requests[1][1]["role"], "user")
        payload = json.loads(client.requests[1][1]["content"])
        self.assertEqual(payload["answers"][0]["answer"], answer_text)
        self.assertEqual(payload["report"], report)

    async def test_malformed_review_is_not_user_question(self):
        """검토 응답의 형식 오류를 사용자 정보 부족으로 바꾸지 않는다."""
        report = "처리 결과에 준비 완료.라는 문구가 표시돼야 합니다."
        invalid = review(report); del invalid["results"][0]["comparison"]
        with self.assertRaises(ValueError):
            await VerificationPlanner(Client([condition(report), invalid]), max_review_attempts=1).plan(spec(report), report=report)

    async def test_scope_error_is_not_text_question(self):
        """대상 범위 오류를 문구 확인 질문으로 바꾸지 않는다."""
        report = "처리 결과에 준비 완료.라는 문구가 표시돼야 합니다."
        invalid = review(report); invalid["results"][0].update(status="mismatch", suggested_target="처리 결과 전체")
        with self.assertRaises(ValueError):
            await VerificationPlanner(Client([condition(report), invalid]), max_attempts=1).plan(spec(report), report=report)

    async def test_interactive_command_preserves_rounds(self):
        """검사 명령 전체가 질문과 실제 답변을 다음 분석에 연결한다."""
        from scripts.check_verification_planner import main
        report = "처리 결과에 준비 완료.라는 문구가 표시돼야 합니다."
        answer_text = '처리 결과에는 "준비 완료."가 포함되면 됩니다.'
        first_parsed = spec(report).model_dump(mode="json")
        next_plan = condition(answer_text)
        next_plan["conditions"][0]["source"] = "user_answer"
        client = Client([first_parsed, condition(report, "equals"), review(report, "mismatch"),
                         condition(report, "equals"), review(report, "mismatch"),
                         next_plan, review(answer_text)])
        def answer_current(questions):
            return [ClarificationAnswer(question=questions[0], answer=answer_text)], False
        previous = Path.cwd()
        with TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                with patch("scripts.check_verification_planner.build_model_client", return_value=client), \
                     patch("scripts.check_verification_planner.collect_answers", side_effect=answer_current):
                    await main(report, "", interactive=True)
                folders = list(Path("artifacts").glob("verification-plan-*"))
                self.assertEqual(len(folders), 1)
                first = json.loads((folders[0] / "round-1.json").read_text())
                second = json.loads((folders[0] / "round-2.json").read_text())
                self.assertEqual(first["plan"]["status"], "needs_information")
                self.assertEqual(second["plan"]["status"], "candidate")
                self.assertEqual(second["answers"][0]["answer"], answer_text)
                self.assertEqual(second["report"], report)
                self.assertEqual(len(client.requests), 7)
                self.assertEqual(second["interpretation_mode"], "retained_for_expectation")
                self.assertEqual(first["bug_spec"]["reported_steps"], second["bug_spec"]["reported_steps"])
            finally:
                os.chdir(previous)

    async def test_blank_console_answer_stops(self):
        """빈 답변이면 근거를 만들지 않고 입력을 중단한다."""
        answers, stopped = collect_answers(["정확한 문구는 무엇인가요?"], read=lambda prompt: "")
        self.assertEqual(answers, [])
        self.assertTrue(stopped)

    async def test_console_answer_preserves_question(self):
        """실제 질문과 사용자 답변을 구분해 저장한다."""
        question = "표시돼야 할 문구와 비교 범위를 알려 주세요."
        text = '"처리 완료."가 포함되면 됩니다.'
        answers, stopped = collect_answers([question], read=lambda prompt: text)
        self.assertFalse(stopped)
        self.assertEqual(answers[0].question, question)
        self.assertEqual(answers[0].answer, text)


if __name__ == "__main__":
    with redirect_stdout(StringIO()):
        unittest.main(verbosity=2)
