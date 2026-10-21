"""실제 브라우저에서 검증용 관찰과 값 읽기를 확인한다."""

import asyncio
from pathlib import Path
from uuid import uuid4

from playwright.async_api import (
    Page,
    async_playwright,
)

from app.browser.verification import VerificationBrowser


class TestPageSource:
    """검사용 페이지를 기존 세션과 같은 방식으로 제공한다."""

    def __init__(self, page: Page):
        self.page = page

    def get_page(self, session_id: str) -> Page:
        """검사 세션의 페이지를 반환한다."""
        if session_id != "test":
            raise ValueError("등록되지 않은 검사 세션입니다.")

        return self.page


async def main():
    """입력값, 일반 문구와 잘못된 대상 연결을 검사한다."""
    artifact_dir = (
        Path("artifacts")
        / f"verification-browser-{uuid4().hex}"
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True
        )

        try:
            page = await browser.new_page()

            await page.set_content(
                """
                <!doctype html>
                <html lang="ko">
                <body>
                    <label>
                        메모 제목
                        <input value="주간 계획">
                    </label>

                    <ul aria-label="검색 결과 목록">
                        <li>팀 회의록</li>
                        <li>업무 일정</li>
                    </ul>

                    <button>확인</button>
                    <button>확인</button>
                </body>
                </html>
                """
            )

            tools = VerificationBrowser(
                sessions=TestPageSource(page),
                artifact_dir=artifact_dir,
            )

            observation = await tools.observe("test")
            observation_id = observation["observation_id"]

            title = await tools.read_named(
                "test",
                observation_id,
                "메모 제목",
                "value",
            )

            assert title["status"] == "ready"
            assert title["actual_value"] == "주간 계획"
            print("PASS: ID 없는 입력 요소의 현재 값 읽기")

            results = await tools.read_named(
                "test",
                observation_id,
                "검색 결과 목록",
                "text",
            )

            assert results["status"] == "ready"
            assert results["actual_value"] == (
                "팀 회의록 업무 일정"
            )
            print("PASS: 결과 목록 전체의 표시 문구 읽기")

            incompatible = await tools.read_named(
                "test",
                observation_id,
                "메모 제목",
                "text",
            )

            assert incompatible["status"] == "incompatible"
            print("PASS: 입력값을 text로 읽는 잘못된 연결 거부")

            ambiguous = await tools.read_named(
                "test",
                observation_id,
                "확인",
                "text",
            )

            assert ambiguous["status"] == "ambiguous"
            print("PASS: 같은 이름의 여러 대상 연결 보류")

            missing = await tools.read_named(
                "test",
                observation_id,
                "존재하지 않는 대상",
                "text",
            )

            assert missing["status"] == "not_observed"
            assert missing["actual_value"] is None
            print("PASS: 관찰하지 못한 대상을 정상 결과로 처리하지 않음")

            await page.get_by_label(
                "메모 제목",
                exact=True,
            ).fill("변경된 제목")

            stale = await tools.read_named(
                "test",
                observation_id,
                "메모 제목",
                "value",
            )

            assert stale["status"] == "stale"
            print("PASS: 관찰 이후 변경된 값의 사용 보류")

            refreshed = await tools.observe("test")

            old = await tools.read_named(
                "test",
                observation_id,
                "메모 제목",
                "value",
            )

            assert old["status"] == "stale"
            print("PASS: 이전 관찰 식별자의 사용 거부")

            updated = await tools.read_named(
                "test",
                refreshed["observation_id"],
                "메모 제목",
                "value",
            )

            assert updated["status"] == "ready"
            assert updated["actual_value"] == "변경된 제목"
            print("PASS: 재관찰 후 변경된 값 읽기")

            print(f"저장 위치: {artifact_dir}")

        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())