"""외부 서버나 모델 없이 브라우저 대상 접속 기능을 검증한다."""

import asyncio
from http.server import (
    BaseHTTPRequestHandler,
    ThreadingHTTPServer,
)
from threading import Thread

from playwright.async_api import expect

from app.browser.session import PlaywrightSessions


class TestHandler(BaseHTTPRequestHandler):
    """브라우저 검증에 필요한 임시 HTTP 응답을 제공한다."""

    def do_GET(self):
        """요청 경로에 따라 검증용 페이지를 반환한다."""
        status = 503 if self.path == "/unavailable" else 200

        body = """
        <!doctype html>
        <html lang="ko">
        <meta charset="utf-8">
        <title>브라우저 대상 검증</title>
        <body>
            <h1>검증용 작업 화면</h1>
            <label>
                제목
                <input name="title">
            </label>
        </body>
        </html>
        """.encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8",
        )
        self.send_header(
            "Content-Length",
            str(len(body)),
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """검증 중 HTTP 요청 로그 출력을 생략한다."""
        return


async def check_sessions(base_url: str) -> None:
    """대상 접속과 브라우저 상태 격리를 확인한다.

    Args:
        base_url: 검증 프로그램이 시작한 임시 서버 주소.

    Returns:
        없음.

    Raises:
        AssertionError: 검증 조건을 충족하지 못한 경우.
    """
    async with PlaywrightSessions(
        targets={
            "workspace": f"{base_url}/workspace"
        }
    ) as sessions:
        first_id = await sessions.open(
            run_id="direct-url",
            start_url=f"{base_url}/editor",
        )
        first_page = sessions.get_page(first_id)

        await expect(
            first_page.get_by_role("heading")
        ).to_have_text("검증용 작업 화면")

        assert first_page.url == f"{base_url}/editor"
        print("PASS: 직접 지정한 URL 접속")

        await first_page.evaluate(
            "localStorage.setItem('session_probe', 'first')"
        )

        await sessions.sessions[first_id].context.add_cookies([
            {
                "name": "session_probe",
                "value": "first",
                "url": base_url,
            }
        ])

        second_id = await sessions.open(
            run_id="registered-target",
            target_id="workspace",
        )
        second_page = sessions.get_page(second_id)

        assert second_page.url == f"{base_url}/workspace"
        print("PASS: 설정에 등록한 대상 접속")

        stored_value = await second_page.evaluate(
            "localStorage.getItem('session_probe')"
        )
        assert stored_value is None

        cookies = await sessions.sessions[
            second_id
        ].context.cookies()

        assert not any(
            item["name"] == "session_probe"
            for item in cookies
        )
        print("PASS: 쿠키와 로컬 저장소 격리")

        error_id = await sessions.open(
            run_id="http-error",
            start_url=f"{base_url}/unavailable",
        )

        assert (
            sessions.sessions[error_id].initial_status
            == 503
        )
        print("PASS: HTTP 오류 응답 상태 보존")

        count_before = len(sessions.sessions)

        invalid_requests = [
            {},
            {
                "target_id": "workspace",
                "start_url": f"{base_url}/editor",
            },
            {"target_id": "not_registered"},
            {"start_url": "file:///tmp/example.html"},
        ]

        for arguments in invalid_requests:
            try:
                await sessions.open(
                    run_id="invalid-request",
                    **arguments,
                )
            except ValueError:
                pass
            else:
                raise AssertionError(
                    "잘못된 대상 지정을 허용했습니다."
                )

        assert len(sessions.sessions) == count_before
        print("PASS: 잘못된 대상 지정 거부")

    assert first_page.is_closed()
    assert second_page.is_closed()
    assert not sessions.sessions

    print("PASS: 브라우저 세션 정리")


def main() -> None:
    """임시 서버를 시작하고 검증 후 정리한다.

    Returns:
        없음.

    Notes:
        운영 서비스와 모델은 호출하지 않는다.
        테스트 서버는 사용 가능한 임시 포트를 사용한다.
    """
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        TestHandler,
    )

    thread = Thread(
        target=server.serve_forever,
        daemon=True,
    )
    thread.start()

    base_url = (
        f"http://127.0.0.1:{server.server_port}"
    )

    try:
        asyncio.run(check_sessions(base_url))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()