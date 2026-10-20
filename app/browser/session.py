"""지정한 서비스에 접속하고 작업별 브라우저 환경을 관리한다."""

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from pydantic import HttpUrl, TypeAdapter

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from app.browser.targets import legacy_demo_targets


URL_ADAPTER = TypeAdapter(HttpUrl)


class BrowserSessions(Protocol):
    """브라우저 세션 관리 기능의 인터페이스."""

    async def open(
        self,
        run_id: str,
        target_id: str | None = None,
        *,
        start_url: str | None = None,
    ) -> str: ...

    async def close(self, session_id: str) -> None: ...


@dataclass
class BrowserSession:
    """하나의 재현 작업이 사용하는 브라우저 환경.

    Attributes:
        run_id: 재현 작업 식별자.
        context: 쿠키·저장소가 분리된 브라우저 환경.
        page: 실제 조작할 페이지.
        start_url: 접속을 시작한 주소.
        initial_status: 최초 접속의 HTTP 응답 상태.
    """

    run_id: str
    context: BrowserContext
    page: Page
    start_url: str
    initial_status: int | None


class PlaywrightSessions:
    """브라우저 시작·대상 접속·세션 정리를 담당한다."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        version: str = "buggy",
        headless: bool = True,
        targets: dict[str, str] | None = None,
    ):
        """세션 관리자를 준비한다.

        Args:
            base_url: 기존 데모 검증을 위한 기본 주소.
            version: 기존 데모 검증을 위한 버전.
            headless: 브라우저 창을 표시하지 않을지 여부.
            targets: 등록 대상의 식별자와 시작 주소.

        Notes:
            targets가 지정되면 해당 설정을 사용한다.
            빈 딕셔너리를 전달하면 등록 대상 없이 URL로만 접속한다.
            기존 호출 호환성을 위해 base_url과 version을 유지한다.
        """
        configured_targets = (
            legacy_demo_targets(base_url, version)
            if targets is None
            else targets
        )

        self.targets = {
            name: str(URL_ADAPTER.validate_python(url))
            for name, url in configured_targets.items()
        }

        self.headless = headless
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.sessions: dict[str, BrowserSession] = {}

    async def __aenter__(self):
        """브라우저를 시작한다.

        Returns:
            시작된 세션 관리자.

        Raises:
            RuntimeError: 이미 시작된 관리자를 다시 사용하는 경우.
        """
        if self.playwright is not None:
            raise RuntimeError(
                "이미 시작된 세션 관리자는 다시 시작할 수 없습니다."
            )

        self.playwright = await async_playwright().start()

        try:
            self.browser = await self.playwright.chromium.launch(
                headless=self.headless
            )
        except BaseException:
            await self.playwright.stop()
            self.playwright = None
            raise

        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        """작업 종료 시 개별 세션과 브라우저를 정리한다.

        Args:
            exc_type: 블록 내부 예외의 종류.
            exc_value: 발생한 예외.
            traceback: 예외 추적 정보.

        Returns:
            없음. 블록 내부 예외를 숨기지 않는다.
        """
        try:
            for session_id in list(self.sessions):
                await self.close(session_id)
        finally:
            try:
                if self.browser is not None:
                    await self.browser.close()
            finally:
                self.sessions.clear()
                self.browser = None

                if self.playwright is not None:
                    await self.playwright.stop()
                    self.playwright = None

    def _resolve_url(
        self,
        target_id: str | None,
        start_url: str | None,
    ) -> str:
        """직접 주소 또는 등록 대상으로 시작 주소를 결정한다.

        Args:
            target_id: 등록된 대상의 식별자.
            start_url: 직접 지정한 시작 주소.

        Returns:
            검증된 HTTP 또는 HTTPS 주소.

        Raises:
            ValueError: 대상 지정이 없거나 중복되거나 등록되지 않은 경우.
            pydantic.ValidationError: 주소 형식이 잘못된 경우.
        """
        if (target_id is None) == (start_url is None):
            raise ValueError(
                "target_id 또는 start_url 중 하나만 지정하세요."
            )

        if start_url is not None:
            return str(
                URL_ADAPTER.validate_python(start_url)
            )

        if target_id not in self.targets:
            raise ValueError(
                f"등록되지 않은 대상입니다: {target_id}"
            )

        return self.targets[target_id]

    async def open(
        self,
        run_id: str,
        target_id: str | None = None,
        *,
        start_url: str | None = None,
    ) -> str:
        """독립된 환경을 만들고 지정한 서비스에 접속한다.

        Args:
            run_id: 재현 작업 식별자.
            target_id: 등록된 대상 식별자.
            start_url: 직접 지정한 시작 주소.

        Returns:
            생성한 브라우저 세션의 식별자.

        Raises:
            RuntimeError: 브라우저가 시작되지 않은 경우.
            ValueError: 대상 지정이 잘못된 경우.
            playwright.async_api.Error: 브라우저 접속에 실패한 경우.

        Notes:
            HTTP 오류 응답도 조사 자료이므로 상태를 보관한다.
            접속 성공 자체가 서비스 정상 동작을 뜻하지 않는다.
        """
        if self.browser is None:
            raise RuntimeError(
                "async with 안에서 사용해야 합니다."
            )

        url = self._resolve_url(
            target_id=target_id,
            start_url=start_url,
        )

        context = await self.browser.new_context(
            viewport={"width": 1000, "height": 900}
        )

        try:
            page = await context.new_page()
            page.set_default_timeout(5000)

            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=10000,
            )

            session_id = uuid4().hex

            self.sessions[session_id] = BrowserSession(
                run_id=run_id,
                context=context,
                page=page,
                start_url=url,
                initial_status=(
                    response.status
                    if response is not None
                    else None
                ),
            )

            return session_id

        except BaseException:
            await context.close()
            raise

    def get_page(self, session_id: str) -> Page:
        """세션에 해당하는 페이지를 반환한다.

        Args:
            session_id: 열린 세션 식별자.

        Returns:
            실제 조작할 페이지.

        Raises:
            ValueError: 세션이 없거나 이미 종료된 경우.
        """
        if session_id not in self.sessions:
            raise ValueError(
                "존재하지 않거나 종료된 세션입니다."
            )

        return self.sessions[session_id].page

    async def close(self, session_id: str) -> None:
        """개별 세션을 종료한다.

        Args:
            session_id: 종료할 세션 식별자.

        Returns:
            없음. 이미 종료된 세션은 다시 닫지 않는다.
        """
        session = self.sessions.pop(session_id, None)

        if session is not None:
            await session.context.close()