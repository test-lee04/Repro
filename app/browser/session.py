"""브라우저 실행과 재현 작업별 독립 환경을 관리한다."""

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)


class BrowserSessions(Protocol):
    async def open(self, run_id: str, target_id: str) -> str: ...

    async def close(self, session_id: str) -> None: ...


@dataclass
class BrowserSession:
    run_id: str
    context: BrowserContext
    page: Page


class PlaywrightSessions:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        version: str = "buggy",
        headless: bool = True,
    ):
        if version not in {"buggy", "fixed"}:
            raise ValueError("version은 buggy 또는 fixed여야 합니다.")

        self.target_url = (
            f"{base_url.rstrip('/')}/demo?version={version}"
        )
        self.headless = headless

        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.sessions: dict[str, BrowserSession] = {}

    async def __aenter__(self):
        """async with 진입 시 브라우저를 시작한다."""
        self.playwright = await async_playwright().start()

        try:
            self.browser = await self.playwright.chromium.launch(
                headless=self.headless
            )
        except BaseException:
            await self.playwright.stop()
            raise

        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        """오류가 발생해도 브라우저 자원을 정리한다."""
        try:
            for session_id in list(self.sessions):
                await self.close(session_id)
        finally:
            try:
                if self.browser is not None:
                    await self.browser.close()
            finally:
                if self.playwright is not None:
                    await self.playwright.stop()

    async def open(self, run_id: str, target_id: str) -> str:
        """재현 작업을 위한 독립 context와 page를 생성한다."""
        if target_id != "demo_cart":
            raise ValueError("현재는 demo_cart만 지원합니다.")

        if self.browser is None:
            raise RuntimeError("async with 안에서 사용해야 합니다.")

        context = await self.browser.new_context(
            viewport={"width": 1000, "height": 900}
        )

        try:
            page = await context.new_page()
            page.set_default_timeout(5000)

            response = await page.goto(
                self.target_url,
                wait_until="domcontentloaded",
                timeout=10000,
            )

            if response is None or not response.ok:
                raise RuntimeError(
                    "데모 페이지에 정상적으로 접속하지 못했습니다."
                )

            session_id = uuid4().hex
            self.sessions[session_id] = BrowserSession(
                run_id=run_id,
                context=context,
                page=page,
            )

            return session_id

        except BaseException:
            await context.close()
            raise

    def get_page(self, session_id: str) -> Page:
        """세션 ID에 해당하는 페이지를 반환한다."""
        if session_id not in self.sessions:
            raise ValueError("존재하지 않거나 종료된 세션입니다.")

        return self.sessions[session_id].page

    async def close(self, session_id: str) -> None:
        """세션을 종료한다. 이미 종료된 경우에는 아무 작업도 하지 않는다."""
        session = self.sessions.pop(session_id, None)

        if session is not None:
            await session.context.close()