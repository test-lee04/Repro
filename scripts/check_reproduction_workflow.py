"""통합 실행 흐름의 검증 규칙과 실제 모델·브라우저 연결을 검사한다."""

import argparse
import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from app.agent.checkpoint_reviewer import (
    CheckpointReview, CheckpointReviewer, StepCoverage,
)
from app.agent.report_schema import BugSpec
from app.agent.reproduction_workflow import ReproductionWorkflow
from app.agent.reproduction_assessor import ReproductionAssessor
from app.agent.target_binder import TargetBinding
from app.agent.verification_schema import VerificationPlan
from app.testing.check_schema import VerificationCondition


def fixture_spec():
    """검사에만 사용하는 자료이며 실제 서비스의 분기 목록이 아니다."""
    return BugSpec(
        summary="저장한 제목이 유지되지 않습니다.",
        reported_steps=["제목 입력", "저장", "새로고침"],
        actual_behavior="제목이 비어 있습니다.",
        expected_behavior="제목은 주간 계획이어야 합니다.",
        expectation_source="report", expectation_evidence="제목은 주간 계획이어야 합니다.",
    )


def fixture_condition():
    return VerificationCondition(
        condition_id="title", kind="value", target_description="문서 제목 입력란",
        operator="equals", expected_value="주간 계획", source="report",
        evidence="제목은 주간 계획이어야 합니다.", checkpoint="저장 후 새로고침한 직후",
    )


def fixture_review(status="reached"):
    return CheckpointReview(
        status=status,
        coverage=[StepCoverage(step_number=index, action_numbers=[index], reason="실행 기록")
                  for index in range(1, 4)] if status == "reached" else [],
        checkpoint_action_numbers=[3] if status == "reached" else [],
        reason="행동 기록을 확인했습니다." if status == "reached" else "근거가 부족합니다.",
    )


class FixtureComponents:
    """실행 흐름의 연결 규칙을 확인하기 위한 구성 요소 대역."""

    def __init__(self, actual="", loop_status="finish_selected", reached=True,
                 binding_status="bound", read_status="ready", delay=0, assessment_status="inconclusive"):
        self.actual, self.loop_status, self.reached = actual, loop_status, reached
        self.binding_status, self.read_status, self.delay = binding_status, read_status, delay
        self.read_calls = 0
        self.bind_calls = 0
        self.parse_calls = 0
        self.needs_information = False
        self.assessment_status = assessment_status
        self.assess_calls = 0

    async def parse(self, **kwargs):
        self.parse_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return {"spec": fixture_spec()}

    async def plan(self, spec, **kwargs):
        return {"plan": VerificationPlan(
            status="needs_information" if self.needs_information else "candidate",
            summary="검사 결과 형식을 준비했습니다.",
            conditions=[] if self.needs_information else [fixture_condition()],
            questions=["정상 동작은 무엇인가요?"] if self.needs_information else [],
            limitation=None,
        )}

    async def run(self, spec, session_id, folder):
        history = []
        for kind, element_id, tag, name, value in [
            ("fill", "input-1", "input", "문서 제목", "주간 계획"),
            ("click", "button-1", "button", "저장", None),
            ("reload", "page-1", "page", "현재 페이지", None),
        ]:
            history.append({
                "success": True,
                "action": {"kind": kind, "element_id": element_id, "value": value},
                "before_elements": [{"element_id": element_id, "dom_id": "", "tag": tag,
                                     "name": name, "label": name if tag == "input" else "",
                                     "actions": [kind]}],
            })
        return {"loop_status": self.loop_status, "history": history, "question": None}

    async def review(self, spec, condition, loop_result):
        return {"review": fixture_review("reached" if self.reached else "inconclusive")}

    async def observe(self, session_id):
        return {"observation_id": "o1", "elements": [
            {"element_id": "e1", "name": "문서 제목", "value": self.actual,
             "tag": "input", "supports_value": True},
        ]}

    async def bind(self, condition, observation):
        self.bind_calls += 1
        is_bound = self.binding_status == "bound"
        return {"condition_id": condition.condition_id, "observation_id": "o1",
                "binding": TargetBinding(
                    status=self.binding_status, element_id="e1" if is_bound else None,
                    candidate_ids=["e1"] if is_bound else [], reason="검사용 연결 결과",
                )}

    async def read(self, session_id, observation_id, element_id, kind):
        self.read_calls += 1
        return {"observation_id": observation_id, "element_id": element_id,
                "kind": kind, "status": self.read_status, "actual_value": self.actual}


    async def assess(self, run):
        """자동 호출과 결과 전달을 검사하기 위한 판정 대역이다."""
        self.assess_calls += 1
        return {"status": self.assessment_status, "reason": "검사용 판정 결과",
                "comparisons": [], "symptom_review": None, "attempts": []}


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, components, timeout=1, assessor=None, test_compiler=None):
        with tempfile.TemporaryDirectory() as folder:
            workflow = ReproductionWorkflow(
                components, components, components, components, components, components,
                timeout_seconds=timeout, assessor=assessor if assessor is not None else components,
                test_compiler=test_compiler,
            )
            result = await workflow.run(report="검사용 제보", session_id="test", artifact_dir=folder)
            saved = json.loads((Path(folder) / "workflow_result.json").read_text())
            self.assertEqual(saved["workflow_status"], result["workflow_status"])
            assessment = json.loads((Path(folder) / "assessment.json").read_text())
            self.assertNotEqual(result["reproduction_status"], "pending")
            self.assertEqual(saved["reproduction_status"], result["reproduction_status"])
            self.assertEqual(assessment, saved["assessment"])
            self.assertEqual(assessment["status"], result["reproduction_status"])
            generation = json.loads((Path(folder) / "test_generation.json").read_text())
            self.assertEqual(generation, saved["test_generation"])
            self.assertEqual(generation["replay_status"], "not_run")
            if generation["test_status"] == "generated":
                for key in ["case_path", "test_path"]:
                    self.assertTrue(Path(generation[key]).is_file())
            return result

    async def test_mismatch(self):
        """조건 비교 결과와 최종 재현 판정이 구분돼 전달되는지 확인한다."""
        result = await self.execute(FixtureComponents(actual="", assessment_status="reproduced"))
        self.assertEqual(result["verification_status"], "unsatisfied")
        self.assertEqual(result["reproduction_status"], "reproduced")

    async def test_match(self):
        result = await self.execute(FixtureComponents(actual="주간 계획", assessment_status="not_reproduced"))
        self.assertEqual(result["verification_status"], "satisfied")
        self.assertEqual(result["reproduction_status"], "not_reproduced")

    async def test_finish_alone_is_not_checkpoint(self):
        components = FixtureComponents(reached=False)
        result = await self.execute(components)
        self.assertEqual(result["verification_status"], "inconclusive")
        self.assertEqual(components.bind_calls, 0)

    async def test_execution_limits(self):
        for status in ["time_limit", "action_limit", "model_limit", "tool_error"]:
            with self.subTest(status=status):
                components = FixtureComponents(loop_status=status)
                result = await self.execute(components)
                self.assertEqual(result["workflow_status"], status)
                self.assertEqual(components.bind_calls, 0)
                self.assertEqual(components.assess_calls, 0)

    async def test_missing_target(self):
        components = FixtureComponents(binding_status="not_observed")
        result = await self.execute(components)
        self.assertEqual(result["verification_status"], "inconclusive")
        self.assertEqual(components.read_calls, 0)

    async def test_stale_read(self):
        result = await self.execute(FixtureComponents(read_status="stale"))
        self.assertEqual(result["verification_status"], "inconclusive")

    async def test_clarification(self):
        components = FixtureComponents()
        components.needs_information = True
        result = await self.execute(components)
        self.assertEqual(result["workflow_status"], "needs_information")
        self.assertTrue(result["questions"])
        self.assertEqual(result["reproduction_status"], "needs_information")

    async def test_global_timeout(self):
        result = await self.execute(FixtureComponents(delay=1), timeout=0.01)
        self.assertEqual(result["workflow_status"], "time_limit")


    async def test_real_assessor_with_mocked_model(self):
        """실제 판정기에 모델 대역을 연결해 정상과 버그 결과를 확인한다."""
        for actual, match, expected in [
            ("", "matched", "reproduced"),
            ("주간 계획", "not_matched", "not_reproduced"),
        ]:
            with self.subTest(expected=expected):
                class Client:
                    async def generate(self, messages, schema):
                        return {"content": json.dumps({
                            "match": match,
                            "evidence": [{"condition_id": "title", "observed_value": actual}],
                            "reason": "제보 현상과 관찰값을 대조했습니다.",
                        }, ensure_ascii=False), "usage": {}}
                result = await self.execute(
                    FixtureComponents(actual=actual), assessor=ReproductionAssessor(Client())
                )
                self.assertEqual(result["reproduction_status"], expected)
                self.assertEqual(result["assessment_status"], "completed")

    async def test_pending_assessment_rejected(self):
        """최종 판정기가 미완료 상태를 반환하면 보류로 저장한다."""
        result = await self.execute(FixtureComponents(assessment_status="pending"))
        self.assertEqual(result["reproduction_status"], "inconclusive")
        self.assertEqual(result["assessment_status"], "not_completed")

    async def test_assessment_error(self):
        """최종 모델 오류가 재현 성공으로 남지 않는지 확인한다."""
        class BrokenAssessor:
            async def assess(self, run):
                raise RuntimeError("검사용 모델 오류")
        result = await self.execute(FixtureComponents(), assessor=BrokenAssessor())
        self.assertEqual(result["reproduction_status"], "inconclusive")
        self.assertEqual(result["phase"], "assessment")
        self.assertEqual(result["workflow_status"], "error")

    async def test_assessment_timeout(self):
        """최종 판정도 전체 실행 제한 시간에 포함한다."""
        class SlowAssessor:
            async def assess(self, run):
                await asyncio.sleep(1)
        result = await self.execute(FixtureComponents(), timeout=0.02, assessor=SlowAssessor())
        self.assertEqual(result["workflow_status"], "time_limit")
        self.assertEqual(result["phase"], "assessment")
        self.assertEqual(result["reproduction_status"], "inconclusive")

    async def test_cancellation_saves_final_status(self):
        """취소 예외를 유지하면서 일관된 최종 기록을 저장한다."""
        started = asyncio.Event()
        class SlowAssessor:
            async def assess(self, run):
                started.set()
                await asyncio.sleep(10)
        components = FixtureComponents()
        with tempfile.TemporaryDirectory() as folder:
            workflow = ReproductionWorkflow(
                components, components, components, components, components, components,
                assessor=SlowAssessor(),
            )
            task = asyncio.create_task(workflow.run(
                report="검사용 제보", session_id="test", artifact_dir=folder
            ))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            saved = json.loads((Path(folder) / "workflow_result.json").read_text())
            assessment = json.loads((Path(folder) / "assessment.json").read_text())
            self.assertEqual(saved["workflow_status"], "cancelled")
            self.assertEqual(saved["reproduction_status"], "inconclusive")
            self.assertEqual(saved["assessment"], assessment)


    async def test_automatic_test_generation(self):
        """확정한 재현과 미재현 실행에서 테스트 파일을 자동 생성한다."""
        for actual, status in [("", "reproduced"), ("주간 계획", "not_reproduced")]:
            with self.subTest(status=status):
                result = await self.execute(FixtureComponents(actual=actual, assessment_status=status))
                self.assertEqual(result["test_generation"]["test_status"], "generated")
                self.assertEqual(result["test_generation"]["action_count"], 3)
                self.assertEqual(result["test_generation"]["assertion_count"], 1)
                self.assertEqual(result["reproduction_status"], status)

    async def test_inconclusive_skips_generation(self):
        """보류한 결과에 확정 테스트 파일을 붙이지 않는다."""
        result = await self.execute(FixtureComponents())
        generation = result["test_generation"]
        self.assertEqual(generation["test_status"], "not_generated")
        self.assertEqual(generation["generation_status"], "skipped")
        self.assertIsNone(generation["test_path"])

    async def test_unsupported_locator_preserves_assessment(self):
        """위치를 다시 찾을 수 없어도 재현 판정을 덮어쓰지 않는다."""
        class AnonymousComponents(FixtureComponents):
            async def observe(self, session_id):
                observation = await super().observe(session_id)
                observation["elements"][0]["name"] = ""
                return observation
        result = await self.execute(AnonymousComponents(assessment_status="reproduced"))
        self.assertEqual(result["reproduction_status"], "reproduced")
        self.assertEqual(result["test_generation"]["test_status"], "unsupported")

    async def test_export_failure_preserves_assessment(self):
        """파일 생성 오류를 제보 재현 여부와 분리한다."""
        class BrokenCompiler:
            def compile_run(self, run):
                return {}
            def export(self, artifact, folder):
                raise OSError("검사용 파일 쓰기 오류")
        result = await self.execute(
            FixtureComponents(assessment_status="reproduced"), test_compiler=BrokenCompiler()
        )
        self.assertEqual(result["reproduction_status"], "reproduced")
        self.assertEqual(result["workflow_status"], "conditions_checked")
        self.assertIsNone(result["error"])
        self.assertEqual(result["test_generation"]["generation_status"], "error")
        self.assertEqual(result["test_generation"]["test_status"], "environment_error")

    async def test_generation_waits_for_assessment(self):
        """최종 판정을 마치기 전에 생성기를 호출하지 않는다."""
        class MustNotRun:
            def compile_run(self, run):
                raise AssertionError("판정이 끝나지 않았는데 생성기를 호출했습니다.")
        result = await self.execute(
            FixtureComponents(assessment_status="pending"), test_compiler=MustNotRun()
        )
        self.assertEqual(result["test_generation"]["generation_status"], "skipped")
        self.assertEqual(result["error"]["type"], "ValueError")

    async def test_generation_does_not_claim_validation(self):
        """생성 상태를 재실행 완료나 수정 검증 완료로 표시하지 않는다."""
        result = await self.execute(FixtureComponents(assessment_status="reproduced"))
        self.assertEqual(result["test_generation"]["test_status"], "generated")
        self.assertEqual(result["test_generation"]["replay_status"], "not_run")


class CheckpointTests(unittest.TestCase):
    def test_complete_references(self):
        CheckpointReviewer.validate_references(
            fixture_review(), fixture_spec().reported_steps, [{"success": True}] * 3
        )

    def test_missing_step(self):
        review = fixture_review()
        review.coverage.pop()
        with self.assertRaises(ValueError):
            CheckpointReviewer.validate_references(review, ["a", "b", "c"], [{"success": True}] * 3)

    def test_failed_action(self):
        with self.assertRaises(ValueError):
            CheckpointReviewer.validate_references(
                fixture_review(), ["a", "b", "c"],
                [{"success": True}, {"success": False}, {"success": True}],
            )

    def test_reused_action(self):
        review = fixture_review()
        review.coverage[1].action_numbers = [1]
        with self.assertRaises(ValueError):
            CheckpointReviewer.validate_references(review, ["a", "b", "c"], [{"success": True}] * 3)

    def test_invented_action(self):
        review = fixture_review()
        review.coverage[-1].action_numbers = [4]
        with self.assertRaises(ValueError):
            CheckpointReviewer.validate_references(review, ["a", "b", "c"], [{"success": True}] * 3)

    def test_action_after_checkpoint(self):
        with self.assertRaises(ValueError):
            CheckpointReviewer.validate_references(
                fixture_review(), ["a", "b", "c"], [{"success": True}] * 4
            )


class ReviewerContractTests(unittest.IsolatedAsyncioTestCase):
    def execution(self):
        history = []
        for kind, element_id, name, value in [
            ("fill", "title", "문서 제목", "주간 계획"),
            ("click", "save", "저장", None),
            ("reload", "page", "현재 페이지", None),
        ]:
            history.append({
                "action": {"kind": kind, "element_id": element_id, "value": value},
                "success": True, "error_type": None,
                "before_elements": [{"element_id": element_id, "name": name,
                                     "tag": "input" if kind == "fill" else "button"}],
            })
        return {"loop_status": "finish_selected", "history": history}

    def client(self, reviews):
        class Client:
            def __init__(self):
                self.calls = []

            async def generate(self, messages, schema):
                self.calls.append(json.loads(json.dumps(messages)))
                return {"content": reviews.pop(0).model_dump_json(), "usage": {}}
        return Client()

    async def test_input_contains_execution_references(self):
        client = self.client([fixture_review()])
        result = await CheckpointReviewer(client).review(
            fixture_spec(), fixture_condition(), self.execution()
        )
        self.assertEqual(result["review"].status, "reached")
        payload = json.loads(client.calls[0][1]["content"])
        self.assertEqual([item["kind"] for item in payload["actions"]],
                         ["fill", "click", "reload"])
        self.assertNotIn("expected_value", payload)
        self.assertNotIn("actual_behavior", payload)

    async def test_retry_revalidates_references(self):
        invalid = fixture_review()
        invalid.coverage[-1].action_numbers = [99]
        client = self.client([invalid, fixture_review()])
        result = await CheckpointReviewer(client).review(
            fixture_spec(), fixture_condition(), self.execution()
        )
        self.assertEqual(len(result["attempts"]), 2)
        self.assertIsNotNone(result["attempts"][0]["validation_error"])

    async def test_no_history_does_not_call_model(self):
        client = self.client([])
        result = await CheckpointReviewer(client).review(
            fixture_spec(), fixture_condition(), {"loop_status": "finish_selected", "history": []}
        )
        self.assertEqual(result["review"].status, "inconclusive")
        self.assertEqual(client.calls, [])


def start_demo(version):
    """검사용 서버를 실행하며 데모 버전은 Agent에 전달하지 않는다."""
    save = "localStorage.setItem('saved_title', titleInput.value);" if version == "fixed" else ""
    html = '''<!doctype html><html lang="ko"><body>
        <label>문서 제목<input id="titleInput"></label>
        <button onclick="saveTitle()">저장</button>
        <output id="message"></output>
        <script>
        const titleInput = document.getElementById('titleInput');
        titleInput.value = localStorage.getItem('saved_title') || '';
        function saveTitle() {
            SAVE_SCRIPT
            document.getElementById('message').innerText = '저장했습니다.';
        }
        </script></body></html>'''.replace("SAVE_SCRIPT", save).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/"


async def live(args):
    """입력한 URL 또는 검사용 화면에서 동일한 실행 흐름을 사용한다."""
    from dotenv import load_dotenv
    from app.agent.action_planner import ActionPlanner
    from app.agent.model_client import build_model_client
    from app.agent.report_parser import ReportParser
    from app.agent.reproduction_loop import ReproductionLoop
    from app.agent.target_binder import TargetBinder
    from app.agent.verification_planner import VerificationPlanner
    from app.browser.session import PlaywrightSessions
    from app.browser.tools import PlaywrightTools
    from app.browser.verification import VerificationBrowser

    load_dotenv()
    server = None
    if args.demo:
        server, url = start_demo(args.demo)
    else:
        url = args.url
    folder = Path("artifacts") / f"workflow-{uuid4().hex}"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        client = build_model_client()
        async with PlaywrightSessions(targets={}) as sessions:
            session_id = await sessions.open(run_id="workflow", start_url=url)
            workflow = ReproductionWorkflow(
                ReportParser(client), VerificationPlanner(client),
                ReproductionLoop(ActionPlanner(client), PlaywrightTools(sessions, folder)),
                CheckpointReviewer(client), TargetBinder(client, diagnostic_dir=folder),
                VerificationBrowser(sessions, folder), timeout_seconds=args.timeout,
                assessor=ReproductionAssessor(client),
            )
            result = await workflow.run(
                report=args.report, public_spec=args.public_spec,
                session_id=session_id, artifact_dir=folder,
            )
        print("실행 종료 상태:", result["workflow_status"])
        print("조건 검증 결과:", result["verification_status"])
        print("최종 재현 상태:", result["reproduction_status"])
        print("판정 이유:", result["assessment"]["reason"])
        generation = result["test_generation"]
        print("테스트 생성 상태:", generation["test_status"])
        print("재실행 상태:", generation["replay_status"])
        print("테스트 생성 설명:", generation["reason"])
        if generation["case_path"]:
            print("테스트 자료:", generation["case_path"])
            print("실행 코드:", generation["test_path"])
        if generation["error"]:
            print("테스트 생성 오류:", generation["error"])
        for check in result["checks"]:
            print(json.dumps(check, ensure_ascii=False, indent=2))
        if result["questions"]:
            print("추가 질문:", result["questions"])
        if result["error"]:
            print("오류:", json.dumps(result["error"], ensure_ascii=False, indent=2))
        print("총 소요 시간:", result["elapsed_seconds"], "초")
        print("저장 위치:", folder)
        print("실행 결과와 최종 판정 근거를 함께 저장했습니다.")
        if args.demo:
            # 데모의 정답은 검사 스크립트만 알고 있으며 실행 흐름에 전달하지 않는다.
            expected_status = "unsatisfied" if args.demo == "buggy" else "satisfied"
            assert result["workflow_status"] == "conditions_checked", "조건 검사까지 도달하지 못했습니다."
            assert result["verification_status"] == expected_status, "데모의 예상 조건 결과와 다릅니다."
            expected_reproduction = "reproduced" if args.demo == "buggy" else "not_reproduced"
            assert result["reproduction_status"] == expected_reproduction, "데모의 최종 재현 상태가 예상과 다릅니다."
            assert generation["test_status"] == "generated", "회귀 테스트 파일을 생성하지 못했습니다."
            assert generation["replay_status"] == "not_run", "생성을 재실행 검증으로 오인하면 안 됩니다."
            print("PASS: 제보부터 최종 재현 판정과 회귀 테스트 생성까지 자동 연결")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--url")
    target.add_argument("--demo", choices=["buggy", "fixed"])
    parser.add_argument("--report")
    parser.add_argument("--public-spec", default="")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    if args.url or args.demo:
        if not args.report:
            parser.error("--report로 제보 내용을 입력해야 합니다.")
        asyncio.run(live(args))
    else:
        unittest.main(argv=["check_reproduction_workflow"], verbosity=2)
