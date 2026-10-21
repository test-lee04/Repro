"""대상 연결의 응답 검사와 실제 모델 연결을 확인한다."""

import argparse
import asyncio
import json
import unittest
from pathlib import Path
from uuid import uuid4

from app.agent.target_binder import TargetBinder
from app.testing.check_schema import VerificationCondition


def make_condition(
    kind="value",
    description="저장한 문서의 제목 입력란",
    expected_value="주간 계획",
    operator="equals",
    evidence="저장한 제목은 주간 계획이어야 합니다.",
):
    """서비스 실행과 분리된 검사 자료를 만든다."""
    return VerificationCondition(
        condition_id="example_condition",
        kind=kind,
        target_description=description,
        operator=operator,
        expected_value=expected_value,
        source="report",
        evidence=evidence,
        checkpoint="저장 후 새로고침한 시점",
    )


def make_observation():
    """실제 값이 비어 있는 제목과 다른 입력 요소를 준비한다."""
    return {
        "observation_id": "test-observation",
        "truncated": False,
        "elements": [
            {"element_id": "title", "tag": "input", "name": "문서 제목",
             "supports_value": True, "value": "", "text": ""},
            {"element_id": "author", "tag": "input", "name": "작성자",
             "supports_value": True, "value": "주간 계획", "text": ""},
            {"element_id": "results", "tag": "ul", "name": "검색 결과 목록",
             "supports_value": False, "value": None, "text": "팀 회의록"},
        ],
    }


def bound(element_id):
    """선택 응답의 검사 자료를 만든다."""
    return {
        "status": "bound",
        "element_id": element_id,
        "candidate_ids": [element_id],
        "reason": "대상 설명과 요소의 역할이 일치합니다.",
    }


class FakeClient:
    """응답 검사를 재현하기 위한 모델 대역."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def generate(self, messages, schema):
        self.calls.append(json.loads(json.dumps(messages)))
        return {
            "content": json.dumps(self.responses.pop(0), ensure_ascii=False),
            "usage": {"backend": "fake", "model": "fake",
                      "input_tokens": 10, "output_tokens": 5},
        }


class TargetBindingTests(unittest.IsolatedAsyncioTestCase):
    """모델 응답을 코드가 실제로 제한하는지 검사한다."""

    async def test_observed_target(self):
        """관찰된 입력 요소를 연결하고 관찰 식별자를 보존한다."""
        result = await TargetBinder(FakeClient([bound("title")])).bind(
            make_condition(), make_observation()
        )
        self.assertEqual(result["binding"].element_id, "title")
        self.assertEqual(result["observation_id"], "test-observation")

    async def test_unknown_target(self):
        """관찰에 없는 대상을 거부한다."""
        with self.assertRaises(ValueError):
            await TargetBinder(FakeClient([bound("invented")]), 1).bind(
                make_condition(), make_observation()
            )

    async def test_incompatible_target(self):
        """입력값 조건을 일반 표시 요소에 연결하지 못하게 한다."""
        with self.assertRaises(ValueError):
            await TargetBinder(FakeClient([bound("results")]), 1).bind(
                make_condition(), make_observation()
            )

    async def test_duplicate_name(self):
        """같은 이름의 입력 요소가 여러 개면 선택을 보류한다."""
        observation = make_observation()
        observation["elements"][1]["name"] = "문서 제목"
        result = await TargetBinder(FakeClient([bound("title")])).bind(
            make_condition(), observation
        )
        self.assertEqual(result["binding"].status, "ambiguous")
        self.assertIsNone(result["binding"].element_id)

    async def test_retry(self):
        """잘못된 첫 응답을 재시도하고 모든 호출 사용량을 합산한다."""
        result = await TargetBinder(
            FakeClient([bound("invented"), bound("title")])
        ).bind(make_condition(), make_observation())
        self.assertEqual(result["usage"]["model_calls"], 2)
        self.assertEqual(result["usage"]["input_tokens"], 20)
        self.assertIsNotNone(result["attempts"][0]["validation_error"])

    async def test_values_not_forwarded(self):
        """기대값과 현재 값 필드를 모델 입력에서 제외한다."""
        client = FakeClient([bound("title")])
        await TargetBinder(client).bind(make_condition(), make_observation())
        payload = json.loads(client.calls[0][1]["content"])
        self.assertNotIn("expected_value", payload["target"])
        for element in payload["observation"]["elements"]:
            self.assertNotIn("value", element)
            self.assertNotIn("text", element)

    async def test_not_observed_is_not_absence(self):
        """관찰하지 못한 결과를 존재 여부의 거짓 값으로 바꾸지 않는다."""
        client = FakeClient([{
            "status": "not_observed", "element_id": None,
            "candidate_ids": [], "reason": "현재 관찰에서 찾지 못했습니다.",
        }])
        result = await TargetBinder(client).bind(
            make_condition(), make_observation()
        )
        self.assertEqual(result["binding"].status, "not_observed")
        self.assertNotIn("actual_value", result)


    async def test_ambiguous_with_one_candidate_rejected(self):
        """후보가 하나인 모호한 응답을 연결 성공으로 보정하지 않는다."""
        client = FakeClient([{
            "status": "ambiguous", "element_id": None,
            "candidate_ids": ["title"], "reason": "확정하기 어렵습니다.",
        }])
        with self.assertRaises(ValueError):
            await TargetBinder(client, 1).bind(make_condition(), make_observation())

    async def test_state_specific_schema(self):
        """상태별 후보 수와 식별자 제약을 모델에 전달한다."""
        client = FakeClient([bound("title")])
        await TargetBinder(client).bind(make_condition(), make_observation())
        payload = json.loads(client.calls[0][1]["content"])
        branches = payload["output_schema"]["anyOf"]
        by_status = {branch["properties"]["status"]["enum"][0]: branch
                     for branch in branches}
        ambiguous = by_status["ambiguous"]["properties"]
        self.assertEqual(ambiguous["element_id"]["type"], "null")
        self.assertEqual(ambiguous["candidate_ids"]["minItems"], 2)
        self.assertTrue(ambiguous["candidate_ids"]["uniqueItems"])
        bound_schema = by_status["bound"]["properties"]
        self.assertEqual(bound_schema["candidate_ids"]["maxItems"], 1)
        self.assertNotIn("results", bound_schema["element_id"]["enum"])

    async def test_ambiguous_retry(self):
        """잘못된 모호 응답 이후 올바른 응답을 다시 검사한다."""
        client = FakeClient([
            {"status": "ambiguous", "element_id": None,
             "candidate_ids": ["title"], "reason": "확정하기 어렵습니다."},
            bound("title"),
        ])
        result = await TargetBinder(client).bind(make_condition(), make_observation())
        self.assertEqual(result["binding"].status, "bound")
        self.assertEqual(result["usage"]["model_calls"], 2)


async def live_check():
    """실제 모델이 고른 대상을 실제 브라우저 읽기에 전달한다."""
    from dotenv import load_dotenv
    from playwright.async_api import async_playwright
    from app.agent.model_client import build_model_client
    from app.browser.verification import VerificationBrowser

    load_dotenv()
    folder = Path("artifacts") / f"target-binding-{uuid4().hex}"
    folder.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content('''
                <!doctype html><html lang="ko"><body>
                <label>문서 제목<input value=""></label>
                <label>작성자<input value="주간 계획"></label>
                <ul aria-label="검색 결과 목록">
                    <li>팀 회의록</li><li>업무 일정</li>
                </ul></body></html>
            ''')

            class PageSource:
                def get_page(self, session_id):
                    return page

            tools = VerificationBrowser(PageSource(), folder)
            binder = TargetBinder(build_model_client(), diagnostic_dir=folder)
            cases = [
                (make_condition(), "문서 제목", ""),
                (make_condition(
                    "text", "검색된 항목을 담은 결과 목록 전체",
                    "회의록", "contains",
                    "검색 결과 목록에는 회의록이 포함돼야 합니다.",
                ),
                 "검색 결과 목록", "팀 회의록 업무 일정"),
            ]
            for index, (condition, expected_name, expected_actual) in enumerate(cases, 1):
                observation = await tools.observe("test")
                result = await binder.bind(condition, observation)
                binding = result["binding"]
                saved = {**result, "binding": binding.model_dump(mode="json")}
                path = folder / f"binding-{index}.json"
                path.write_text(json.dumps(saved, ensure_ascii=False, indent=2),
                                encoding="utf-8")
                print("대상 연결 결과:")
                print(binding.model_dump_json(indent=2))
                assert binding.status == "bound", "대상을 확정하지 못했습니다."
                selected = next(element for element in observation["elements"]
                                if element["element_id"] == binding.element_id)
                assert selected["name"] == expected_name, "다른 대상을 선택했습니다."
                read_result = await tools.read(
                    "test", result["observation_id"], binding.element_id, condition.kind
                )
                saved["read_result"] = read_result
                path.write_text(json.dumps(saved, ensure_ascii=False, indent=2),
                                encoding="utf-8")
                assert read_result["status"] == "ready"
                assert read_result["actual_value"] == expected_actual
                print("실제 읽은 값:", repr(read_result["actual_value"]))
                print("모델 사용량:", result["usage"])
                print(f"PASS: 실제 모델의 대상 연결 및 {condition.kind} 읽기")
            print(f"저장 위치: {folder}")
            print("이번 검사는 대상 연결과 읽기이며 버그 판정은 아닙니다.")
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.live:
        asyncio.run(live_check())
    else:
        unittest.main(argv=["check_target_binding"], verbosity=2)
