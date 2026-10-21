"""Check comparison rules and the browser-to-comparison integration."""

import argparse
import asyncio
import json
import unittest
from pathlib import Path
from uuid import uuid4

from app.testing.check_schema import VerificationCondition
from app.testing.condition_comparator import compare_condition


def make_condition(kind="value", operator="equals", expected="주간 계획"):
    """Build test data, never runtime bug routing rules."""
    return VerificationCondition(
        condition_id="example", kind=kind,
        target_description="검사 대상", operator=operator,
        expected_value=expected, source="report",
        evidence="제공된 기대 동작을 확인합니다.",
        checkpoint="검사용 화면을 준비한 후",
    )


def make_records(actual="", status="ready"):
    """Build synthetic records for comparator contract tests."""
    binding = {
        "condition_id": "example", "observation_id": "observation-1",
        "binding": {"status": "bound", "element_id": "element-1",
                    "candidate_ids": ["element-1"], "reason": "연결했습니다."},
    }
    read = {
        "observation_id": "observation-1", "element_id": "element-1",
        "kind": "value", "status": status, "actual_value": actual,
    }
    return binding, read


class ComparisonTests(unittest.TestCase):
    """Ensure unsupported or incomplete evidence never becomes a mismatch."""

    def test_exact_match(self):
        """같은 입력값은 충족으로 판정한다."""
        binding, read = make_records("주간 계획")
        result = compare_condition(make_condition(), binding, read, checkpoint_reached=True)
        self.assertEqual(result.status, "satisfied")

    def test_empty_value_is_real_value(self):
        """읽은 빈 문자열을 누락된 값과 구분한다."""
        binding, read = make_records("")
        result = compare_condition(make_condition(), binding, read, checkpoint_reached=True)
        self.assertEqual(result.status, "unsatisfied")
        self.assertEqual(result.actual_value, "")

    def test_contains(self):
        """목록 문구에 기대 문자열이 포함되는지 비교한다."""
        binding, read = make_records("팀 회의록 업무 일정")
        read["kind"] = "text"
        result = compare_condition(
            make_condition("text", "contains", "회의록"), binding, read,
            checkpoint_reached=True,
        )
        self.assertEqual(result.status, "satisfied")

    def test_missing_substring(self):
        """읽기는 성공했지만 기대 문구가 없으면 불충족이다."""
        binding, read = make_records("업무 일정")
        read["kind"] = "text"
        result = compare_condition(
            make_condition("text", "contains", "회의록"), binding, read,
            checkpoint_reached=True,
        )
        self.assertEqual(result.status, "unsatisfied")

    def test_no_silent_trimming(self):
        """입력값의 공백 차이를 임의로 제거하지 않는다."""
        binding, read = make_records("주간 계획 ")
        result = compare_condition(make_condition(), binding, read, checkpoint_reached=True)
        self.assertEqual(result.status, "unsatisfied")
        self.assertEqual(result.actual_value, "주간 계획 ")

    def test_checkpoint_required(self):
        """검사 시점이 확인되지 않으면 비교를 보류한다."""
        binding, read = make_records("")
        result = compare_condition(make_condition(), binding, read)
        self.assertEqual(result.status, "inconclusive")

    def test_incomplete_binding(self):
        """대상 미관찰과 모호함을 불충족으로 처리하지 않는다."""
        for status in ["not_observed", "ambiguous"]:
            with self.subTest(status=status):
                binding, read = make_records("")
                binding["binding"].update(
                    status=status, element_id=None,
                    candidate_ids=["a", "b"] if status == "ambiguous" else [],
                )
                result = compare_condition(make_condition(), binding, read,
                                           checkpoint_reached=True)
                self.assertEqual(result.status, "inconclusive")

    def test_unready_reads(self):
        """읽기 오류나 오래된 관찰을 버그로 판정하지 않는다."""
        for status in ["stale", "incompatible", "not_observed", "unsupported"]:
            with self.subTest(status=status):
                binding, read = make_records(None, status)
                result = compare_condition(make_condition(), binding, read,
                                           checkpoint_reached=True)
                self.assertEqual(result.status, "inconclusive")

    def test_mismatched_records(self):
        """다른 조건, 관찰, 대상 또는 읽기 종류를 섞지 않는다."""
        cases = [
            ("binding", "condition_id", "other"),
            ("read", "observation_id", "other"),
            ("read", "element_id", "other"),
            ("read", "kind", "text"),
        ]
        for section, key, value in cases:
            with self.subTest(key=key):
                binding, read = make_records("주간 계획")
                (binding if section == "binding" else read)[key] = value
                result = compare_condition(make_condition(), binding, read,
                                           checkpoint_reached=True)
                self.assertEqual(result.status, "inconclusive")

    def test_missing_records(self):
        """연결이나 읽기 기록이 없으면 보류한다."""
        binding, read = make_records()
        for b, r in [(None, read), (binding, None)]:
            result = compare_condition(make_condition(), b, r, checkpoint_reached=True)
            self.assertEqual(result.status, "inconclusive")

    def test_wrong_actual_type(self):
        """누락값과 숫자를 문자열로 임의 변환하지 않는다."""
        for actual in [None, 0, False, []]:
            with self.subTest(actual=actual):
                binding, read = make_records(actual)
                result = compare_condition(make_condition(), binding, read,
                                           checkpoint_reached=True)
                self.assertEqual(result.status, "inconclusive")

    def test_unsupported_kind(self):
        """아직 읽기를 지원하지 않는 종류를 명시적으로 구분한다."""
        result = compare_condition(
            make_condition("url", "contains", "/notes"), None, None,
            checkpoint_reached=True,
        )
        self.assertEqual(result.status, "unsupported")

    def test_invalid_binding(self):
        """일관성이 깨진 연결 기록을 거부한다."""
        binding, read = make_records()
        binding["binding"]["candidate_ids"] = []
        result = compare_condition(make_condition(), binding, read, checkpoint_reached=True)
        self.assertEqual(result.status, "inconclusive")


async def live_check():
    """Use a prepared browser fixture, not a full reproduction workflow."""
    from dotenv import load_dotenv
    from playwright.async_api import async_playwright
    from app.agent.model_client import build_model_client
    from app.agent.target_binder import TargetBinder
    from app.browser.verification import VerificationBrowser

    load_dotenv()
    folder = Path("artifacts") / f"condition-comparison-{uuid4().hex}"
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
            conditions = [
                ("value", "equals", "주간 계획", "저장한 문서의 제목 입력란", "unsatisfied"),
                ("text", "contains", "회의록", "검색된 항목을 담은 결과 목록 전체", "satisfied"),
            ]
            for index, (kind, operator, expected, description, expected_status) in enumerate(conditions, 1):
                condition = make_condition(kind, operator, expected).model_copy(
                    update={"condition_id": f"fixture-{index}",
                            "target_description": description}
                )
                observation = await tools.observe("test")
                binding_result = await binder.bind(condition, observation)
                binding = binding_result["binding"]
                read_result = None
                if binding.status == "bound":
                    read_result = await tools.read(
                        "test", binding_result["observation_id"], binding.element_id, kind
                    )

                # 검사용 화면을 직접 준비했으므로 이 검사의 시점만 확인한다.
                # 실제 제보의 저장이나 새로고침을 수행했다는 의미가 아니다.
                comparison = compare_condition(
                    condition, binding_result, read_result, checkpoint_reached=True
                )
                saved_binding = {
                    **binding_result,
                    "binding": binding.model_dump(mode="json"),
                }
                record = {
                    "fixture_only": True,
                    "condition": condition.model_dump(mode="json"),
                    "binding_result": saved_binding,
                    "read_result": read_result,
                    "comparison": comparison.model_dump(mode="json"),
                }
                (folder / f"comparison-{index}.json").write_text(
                    json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(comparison.model_dump_json(indent=2))
                assert comparison.status == expected_status, "예상한 비교 결과와 다릅니다."
                print(f"PASS: 실제 연결과 읽기를 거친 {kind} 조건 비교")

                if index == 1:
                    await page.get_by_label("문서 제목", exact=True).fill("변경된 제목")
                    stale_read = await tools.read(
                        "test", binding_result["observation_id"], binding.element_id, kind
                    )
                    held = compare_condition(
                        condition, binding_result, stale_read, checkpoint_reached=True
                    )
                    assert held.status == "inconclusive"
                    (folder / "stale-comparison.json").write_text(
                        held.model_dump_json(indent=2), encoding="utf-8"
                    )
                    print("PASS: 관찰 이후 변경된 대상의 비교 보류")

            print(f"저장 위치: {folder}")
            print("조건 비교 검사이며 실제 제보의 버그 재현 완료는 아닙니다.")
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.live:
        asyncio.run(live_check())
    else:
        unittest.main(argv=["check_condition_comparator"], verbosity=2)
