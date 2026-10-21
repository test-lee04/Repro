"""확인 답변을 사용한 평가의 입력 검증과 실제 브라우저 실행 연결을 검사한다."""

import asyncio
import os
from types import SimpleNamespace
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.agent.prompts import ACTION_PROMPT, CHECKPOINT_PROMPT, SCOPE_REVIEW_PROMPT, SYMPTOM_PROMPT, TARGET_BINDING_PROMPT, VERIFICATION_PROMPT
from eval.confirmation import load_confirmation
from eval.fixtures import CASES, FixtureServer
from eval.run_eval import run_trial, evaluate
from eval.summarize import summarize, write_reports
from scripts.check_evaluation import manifest, sample_row

CASE = next(item for item in CASES if item.case_id == "validation")
ANSWER = '가입 처리 결과 영역에는 "이메일을 입력해 주세요."라는 문구가 포함되면 됩니다. 영역 전체 내용이 정확히 같을 필요는 없습니다.'


def saved_record():
    """실제 답변 형식과 원래 제보를 포함한 검사 입력을 만든다."""
    return {"report": CASE.report, "public_spec": "",
            "answers": [{"question": "정확한 문구와 비교 범위는 무엇인가요?", "answer": ANSWER}],
            "bug_spec": {"summary": "빈 이메일 가입 처리", "preconditions": [
                {"description": "이메일 입력란이 비어 있습니다.", "source": "report", "evidence": "이메일 입력란이 비어 있는 초기 화면"}],
                "reported_steps": ["가입 버튼을 누릅니다."], "inputs": [], "actual_behavior": "가입 완료라고 나옵니다.",
                "expected_behavior": "이메일 입력 안내가 표시돼야 합니다.", "expectation_source": "report",
                "expectation_evidence": CASE.report, "missing_information": []},
            "plan": candidate()}


def candidate():
    """원문에 있는 안내 문구를 확인하는 조건 후보를 만든다."""
    return {"status": "candidate", "summary": "안내 문구 포함 확인", "conditions": [
        {"condition_id": "notice", "kind": "text", "target_description": "가입 처리 결과 영역",
         "operator": "contains", "expected_value": "이메일을 입력해 주세요.", "source": "user_answer",
         "evidence": ANSWER, "checkpoint": "가입 버튼을 누른 후"}], "questions": [], "limitation": None}


class ConfirmationInputTests(unittest.TestCase):
    """다른 제보나 확인되지 않은 답변 기록으로 평가하지 못하게 한다."""

    def load(self, saved, case=CASE):
        """임시 파일로 실제 입력 검증 함수를 호출한다."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "verification_plan.json"
            path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
            return load_confirmation(Path(directory), case)

    def test_valid_saved_answer(self):
        """확정 기록에서 원래 해석과 답변만 반환한다."""
        result = self.load(saved_record())
        self.assertEqual(result["answers"][0].answer, ANSWER)
        self.assertEqual(len(result["fingerprint"]), 64)
        self.assertNotIn("plan", result)
        self.assertNotIn("expected_status", result)

    def test_missing_answer(self):
        """답변이 없는 기록은 답변 후 평가로 사용할 수 없다."""
        data = saved_record(); data["answers"] = []
        with self.assertRaises(ValueError): self.load(data)

    def test_other_report(self):
        """원래 제보가 다른 기록을 섞지 않는다."""
        data = saved_record(); data["report"] = "다른 제보"
        with self.assertRaises(ValueError): self.load(data)

    def test_unconfirmed_plan(self):
        """질문 대기 기록은 확정 기록이 아니다."""
        data = saved_record(); data["plan"] = {"status": "needs_information", "summary": "확인 필요", "conditions": [], "questions": ["문구는 무엇인가요?"], "limitation": None}
        with self.assertRaises(ValueError): self.load(data)

    def test_question_is_not_evidence(self):
        """질문 문구를 사용자 답변 근거로 쓰지 못한다."""
        data = saved_record(); data["plan"]["conditions"][0]["evidence"] = data["answers"][0]["question"]
        with self.assertRaises(ValueError): self.load(data)

    def test_punctuation_loss_rejected(self):
        """저장 기록에서도 인용 문구의 마침표 손실을 허용하지 않는다."""
        data = saved_record(); data["plan"]["conditions"][0]["expected_value"] = "이메일을 입력해 주세요"
        with self.assertRaises(ValueError): self.load(data)

    def test_question_only_case_rejected(self):
        """정보 부족 요청 자체를 정답으로 둔 자료의 의미를 바꾸지 않는다."""
        case = next(item for item in CASES if item.needs_information)
        with self.assertRaises(ValueError): self.load(saved_record(), case)

    def test_mixed_modes_rejected(self):
        """답변 없는 결과와 답변 후 결과를 한 성공률로 섞지 않는다."""
        row = sample_row(); row["evaluation_mode"] = "with_confirmation"
        with self.assertRaises(ValueError): summarize(manifest([row]))

    def test_report_labels_confirmation(self):
        """보고서에 답변 이후 측정과 시간 범위를 표시한다."""
        data = manifest([sample_row()]); data["evaluation_mode"] = "with_confirmation"
        data["timing_scope"] = "after_saved_confirmation"
        with TemporaryDirectory() as directory:
            folder = Path(directory); (folder / "evaluation.json").write_text(json.dumps(data), encoding="utf-8")
            result = write_reports(folder)
            self.assertEqual(result["evaluation_mode"], "with_confirmation")
            self.assertIn("이전 질문 생성과 사용자 답변 시간은 포함하지 않습니다", (folder / "summary.md").read_text())


class ScriptedModel:
    """브라우저 연결 검사용 모델 대역이며 실제 모델의 의미 정확도를 검사하지 않는다."""

    async def generate(self, messages, schema):
        """각 구성 요소가 기대하는 형식만 실제 화면 식별자에 맞춰 반환한다."""
        prompt = messages[0]["content"]
        payload = json.loads(messages[1]["content"])
        if prompt == VERIFICATION_PROMPT:
            response = candidate()
        elif prompt == SCOPE_REVIEW_PROMPT:
            item = payload["conditions"][0]
            response = {"results": [{"condition_id": item["condition_id"], "status": "same_scope", "evidence_id": item["evidence_id"],
                        "reason": "검사 대상 역할을 확인했습니다.", "suggested_target": None,
                        "comparison": {"status": "consistent", "evidence_id": item["evidence_id"], "reason": "원문의 문구 포함 요구입니다."}}]}
        elif prompt == ACTION_PROMPT:
            button = next(item for item in payload["observation"]["elements"] if item.get("name") == "가입" and "click" in item.get("actions", []))
            response = {"decision": "act", "description": "가입 버튼을 클릭합니다.",
                        "action": {"kind": "click", "element_id": button["element_id"], "value": None}, "question": None}
        elif prompt == CHECKPOINT_PROMPT:
            response = {"status": "reached", "coverage": [{"step_number": 1, "action_numbers": [1], "reason": "실제 클릭 기록입니다."}],
                        "checkpoint_action_numbers": [1], "reason": "제보의 클릭 한 번을 확인했습니다."}
        elif prompt == TARGET_BINDING_PROMPT:
            target = next(item for item in payload["observation"]["elements"] if item["tag"] == "output")
            response = {"status": "bound", "element_id": target["element_id"], "candidate_ids": [target["element_id"]], "reason": "결과 영역에 연결했습니다."}
        elif prompt == SYMPTOM_PROMPT:
            item = payload["observations"][0]
            match = item["actual_value"] in payload["reported_actual_behavior"]
            response = {"match": "matched" if match else "not_matched",
                        "evidence": [{"condition_id": item["condition_id"], "observed_value": item["actual_value"]}],
                        "reason": "실제 읽은 결과와 제보 현상을 비교했습니다."}
        else:
            raise AssertionError("예상하지 않은 모델 호출입니다.")
        return {"content": json.dumps(response, ensure_ascii=False),
                "usage": {"backend": "fixture", "model": "scripted", "input_tokens": 10, "output_tokens": 5}}


class ConfirmedTrialTests(unittest.IsolatedAsyncioTestCase):
    """답변 전달 경로와 실제 브라우저 및 회귀 테스트 연결을 확인한다."""

    async def test_only_user_facts_forwarded(self):
        """두 환경에 같은 제보와 답변을 전달하고 정답을 주지 않는다."""
        received = []
        class Server:
            def url(self, *args): return "http://example.test/opaque"
        async def execute(request, trial_id, folder, *, model_client, answers):
            context = json.loads((folder / "interpretation_context.json").read_text())
            received.append({"request": request.model_dump(mode="json"), "context": context,
                             "answers": [item.model_dump() for item in answers]})
            return {"reproduction_status": "inconclusive", "workflow_status": "conditions_checked"}
        with TemporaryDirectory() as directory:
            root = Path(directory); source = root / "verification_plan.json"
            source.write_text(json.dumps(saved_record(), ensure_ascii=False), encoding="utf-8")
            confirmation = load_confirmation(source, CASE)
            with patch("eval.run_eval.build_model_client", return_value=object()), patch("eval.run_eval.execute_workflow", execute):
                for variant in ("buggy", "fixed"):
                    row, cancelled = await run_trial(CASE, variant, 1, Server(), root, 1, False, confirmation=confirmation)
                    self.assertEqual(row["evaluation_mode"], "with_confirmation")
                    self.assertFalse(cancelled)
            self.assertEqual(received[0], received[1])
            encoded = json.dumps(received, ensure_ascii=False)
            for forbidden in ("expected_status", "variant", "buggy", "fixed"):
                self.assertNotIn(forbidden, encoded)

    async def test_complete_confirmed_collection(self):
        """답변 후 평가의 두 시도와 보고서를 별도 방식으로 저장한다."""
        class Server:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def url(self, *args): return "http://example.test/opaque"
        async def execute(*args, **kwargs):
            self.assertEqual(kwargs["answers"][0].answer, ANSWER)
            return {"reproduction_status": "inconclusive", "workflow_status": "conditions_checked"}
        previous = Path.cwd()
        with TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                source = Path(directory) / "verification_plan.json"
                source.write_text(json.dumps(saved_record(), ensure_ascii=False), encoding="utf-8")
                with patch("eval.run_eval.FixtureServer", Server), patch("eval.run_eval.build_model_client", return_value=object()), patch("eval.run_eval.execute_workflow", execute):
                    await evaluate(SimpleNamespace(case="validation", repeats=1, timeout=1,
                                                   verify_tests=False, confirmed_plan=source))
                manifest_path = next(Path("artifacts").glob("evaluation-*/evaluation.json"))
                result = json.loads(manifest_path.read_text())
                self.assertEqual(result["evaluation_mode"], "with_confirmation")
                self.assertEqual(result["timing_scope"], "after_saved_confirmation")
                self.assertEqual(len(result["rows"]), 2)
                self.assertEqual(result["confirmation"]["answer_count"], 1)
                summary = json.loads((manifest_path.parent / "summary.json").read_text())
                self.assertEqual(summary["exact_match"]["total"], 2)
                self.assertEqual(summary["exact_match"]["count"], 0)
            finally:
                os.chdir(previous)

    async def test_one_confirmation_cannot_feed_all_cases(self):
        """한 제보의 확인 답변을 다른 평가 자료까지 적용하지 않는다."""
        with patch("eval.run_eval.build_model_client") as client:
            with self.assertRaises(ValueError):
                await evaluate(SimpleNamespace(case="all", repeats=1, timeout=1,
                                               verify_tests=False, confirmed_plan=Path("unused.json")))
            client.assert_not_called()

    async def test_real_browser_and_same_test_replay(self):
        """모델 대역과 실제 브라우저로 판정, 생성, 원래 실패와 수정 통과를 확인한다."""
        with TemporaryDirectory() as directory:
            root = Path(directory); source = root / "verification_plan.json"
            source.write_text(json.dumps(saved_record(), ensure_ascii=False), encoding="utf-8")
            confirmation = load_confirmation(source, CASE)
            with FixtureServer() as server, patch("eval.run_eval.build_model_client", side_effect=ScriptedModel):
                buggy, cancelled = await run_trial(CASE, "buggy", 1, server, root, 60, True, confirmation=confirmation)
                fixed, _ = await run_trial(CASE, "fixed", 1, server, root, 60, False, confirmation=confirmation)
            self.assertFalse(cancelled)
            self.assertEqual(buggy["actual_status"], "reproduced", buggy.get("error"))
            self.assertEqual(fixed["actual_status"], "not_reproduced", fixed.get("error"))
            self.assertTrue(buggy["test_generated"])
            self.assertTrue(fixed["test_generated"])
            self.assertEqual(buggy["test_status"], "validated", buggy.get("verification_error"))
            self.assertEqual(buggy["action_attempts"], 1)
            self.assertEqual(fixed["action_attempts"], 1)


if __name__ == "__main__":
    with redirect_stdout(StringIO()):
        unittest.main(verbosity=2)
