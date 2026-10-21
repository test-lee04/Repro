"""재현 판정 규칙을 검사하고 저장된 실행 기록에 실제 모델을 적용한다."""

import argparse
import asyncio
import json
import unittest
from pathlib import Path

from app.agent.reproduction_assessor import ReproductionAssessor


def make_run(actual="", read_status="ready"):
    """검사용 실행 기록을 준비한다. 실제 서비스의 분기 규칙이 아니다."""
    condition = {
        "condition_id": "title", "kind": "value",
        "target_description": "문서 제목 입력란", "operator": "equals",
        "expected_value": "주간 계획", "source": "report",
        "evidence": "제목은 주간 계획이어야 합니다.", "checkpoint": "새로고침 직후",
    }
    return {
        "workflow_status": "conditions_checked",
        "interpretation": {"spec": {
            "summary": "제목이 유지되지 않습니다.",
            "reported_steps": ["제목 입력", "저장", "새로고침"],
            "actual_behavior": "제목이 비어 있습니다.",
            "expected_behavior": "제목은 주간 계획이어야 합니다.",
            "expectation_source": "report",
            "expectation_evidence": "제목은 주간 계획이어야 합니다.",
        }},
        "condition_generation": {"plan": {
            "status": "candidate", "summary": "제목 유지 여부 확인",
            "conditions": [condition], "questions": [], "limitation": None,
        }},
        "execution": {"loop_status": "finish_selected", "history": [
            {"success": True}, {"success": True}, {"success": True},
        ]},
        "checks": [{
            "condition": dict(condition),
            "checkpoint": {"review": {
                "status": "reached", "reason": "실행 기록 확인",
                "coverage": [{"step_number": n, "action_numbers": [n], "reason": "근거"}
                             for n in range(1, 4)],
                "checkpoint_action_numbers": [3],
            }},
            "observation": {"observation_id": "o1", "elements": [
                {"element_id": "e1", "name": "문서 제목", "value": actual},
            ]},
            "binding_result": {
                "condition_id": "title", "observation_id": "o1",
                "binding": {"status": "bound", "element_id": "e1",
                            "candidate_ids": ["e1"], "reason": "대상 확인"},
            },
            "read_result": {"observation_id": "o1", "element_id": "e1",
                            "kind": "value", "status": read_status, "actual_value": actual},
        }],
    }


def response(match, actual):
    """모델 응답 검사용 자료를 만든다."""
    return {"match": match, "evidence": [{"condition_id": "title", "observed_value": actual}],
            "reason": "관찰값과 제보 현상을 대조했습니다."}


class FakeClient:
    """외부 모델 없이 판정 규칙과 입력 계약을 검사한다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def generate(self, messages, schema):
        self.calls.append(json.loads(json.dumps(messages)))
        return {"content": json.dumps(self.responses.pop(0), ensure_ascii=False), "usage": {}}


class AssessmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_reproduced(self):
        """조건 불충족과 제보 현상 일치가 함께 있어야 재현이다."""
        result = await ReproductionAssessor(FakeClient([response("matched", "")])).assess(make_run())
        self.assertEqual(result["status"], "reproduced")

    async def test_not_reproduced(self):
        """조건 충족과 제보 현상 불일치를 이번 실행의 미재현으로 구분한다."""
        result = await ReproductionAssessor(
            FakeClient([response("not_matched", "주간 계획")])
        ).assess(make_run("주간 계획"))
        self.assertEqual(result["status"], "not_reproduced")

    async def test_different_failure(self):
        """다른 이상 현상은 제보 재현으로 처리하지 않는다."""
        result = await ReproductionAssessor(
            FakeClient([response("not_matched", "다른 제목")])
        ).assess(make_run("다른 제목"))
        self.assertEqual(result["status"], "inconclusive")

    async def test_conflicting_judgments(self):
        """정상 조건 충족과 현상 일치 판단이 충돌하면 보류한다."""
        result = await ReproductionAssessor(
            FakeClient([response("matched", "주간 계획")])
        ).assess(make_run("주간 계획"))
        self.assertEqual(result["status"], "inconclusive")

    async def test_uncertain(self):
        """현상 의미를 확정할 수 없으면 보류한다."""
        result = await ReproductionAssessor(FakeClient([response("uncertain", "")])).assess(make_run())
        self.assertEqual(result["status"], "inconclusive")

    async def test_unfinished_execution(self):
        for state in ["time_limit", "action_limit", "model_limit", "error"]:
            with self.subTest(state=state):
                run = make_run()
                run["workflow_status"] = state
                client = FakeClient([])
                result = await ReproductionAssessor(client).assess(run)
                self.assertEqual(result["status"], "inconclusive")
                self.assertFalse(client.calls)

    async def test_failed_action(self):
        run = make_run()
        run["execution"]["history"][1]["success"] = False
        result = await ReproductionAssessor(FakeClient([])).assess(run)
        self.assertEqual(result["status"], "inconclusive")

    async def test_stale_read(self):
        result = await ReproductionAssessor(FakeClient([])).assess(make_run(read_status="stale"))
        self.assertEqual(result["status"], "inconclusive")

    async def test_missing_check(self):
        run = make_run()
        run["checks"] = []
        result = await ReproductionAssessor(FakeClient([])).assess(run)
        self.assertEqual(result["status"], "inconclusive")

    async def test_missing_actual_behavior(self):
        run = make_run()
        run["interpretation"]["spec"]["actual_behavior"] = None
        result = await ReproductionAssessor(FakeClient([])).assess(run)
        self.assertEqual(result["status"], "needs_information")

    async def test_fabricated_evidence_rejected(self):
        client = FakeClient([response("matched", "만든 값")])
        result = await ReproductionAssessor(client, 1).assess(make_run())
        self.assertEqual(result["status"], "inconclusive")
        self.assertIsNotNone(result["attempts"][0]["validation_error"])

    async def test_expected_value_not_forwarded(self):
        client = FakeClient([response("matched", "")])
        await ReproductionAssessor(client).assess(make_run())
        payload = json.loads(client.calls[0][1]["content"])
        self.assertEqual(set(payload), {"reported_actual_behavior", "observations", "output_schema"})
        self.assertNotIn("expected_value", payload["observations"][0])


    async def test_wrong_observation(self):
        run = make_run()
        run["checks"][0]["observation"]["observation_id"] = "other"
        result = await ReproductionAssessor(FakeClient([])).assess(run)
        self.assertEqual(result["status"], "inconclusive")

    async def test_retry_evidence(self):
        client = FakeClient([response("matched", "만든 값"), response("matched", "")])
        result = await ReproductionAssessor(client).assess(make_run())
        self.assertEqual(result["status"], "reproduced")
        self.assertEqual(len(result["attempts"]), 2)

    async def test_tool_error(self):
        run = make_run()
        run["workflow_status"] = "tool_error"
        result = await ReproductionAssessor(FakeClient([])).assess(run)
        self.assertEqual(result["status"], "environment_error")


async def main(folder):
    """기존 실행 결과에 실제 모델 판정을 적용하고 별도 파일로 저장한다."""
    from dotenv import load_dotenv
    from app.agent.model_client import build_model_client

    load_dotenv()
    path = Path(folder) / "workflow_result.json"
    run = json.loads(path.read_text(encoding="utf-8"))
    try:
        assessment = await ReproductionAssessor(build_model_client()).assess(run)
    except Exception as error:
        assessment = {"status": "inconclusive", "reason": "현상 대조를 마치지 못했습니다.",
                      "error": {"type": type(error).__name__, "message": str(error)}}
    assessment["source_run"] = str(path)
    output = Path(folder) / "assessment.json"
    output.write_text(json.dumps(assessment, ensure_ascii=False, indent=2), encoding="utf-8")
    print("최종 재현 상태:", assessment["status"])
    print("판정 이유:", assessment["reason"])
    print(json.dumps(assessment, ensure_ascii=False, indent=2))
    print("저장 위치:", output)
    print("현재 사이트가 아닌 저장된 실행 시점의 결과에 대한 판정입니다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir")
    args = parser.parse_args()
    if args.run_dir:
        asyncio.run(main(args.run_dir))
    else:
        unittest.main(argv=["check_reproduction_assessment"], verbosity=2)
