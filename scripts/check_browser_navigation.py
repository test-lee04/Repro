"""링크 클릭과 새로고침의 형식·검증·실행 연결을 검사한다."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit
from uuid import uuid4

from app.agent.action_planner import validate_action_target
from app.agent.action_schema import BrowserAction, NextDecision
from app.browser.session import PlaywrightSessions
from app.browser.tools import PlaywrightTools


PAGE_HTML = """
<!doctype html>
<html lang="ko">
<meta charset="utf-8">
<title>이동 도구 검증</title>
<body>
    <label>메모 <textarea></textarea></label>

    <button onclick="
        const value = document.querySelector('textarea').value;
        localStorage.setItem('memo', value);
        document.querySelector('output').textContent = value;
    ">저장</button>

    <output aria-label="저장 결과">대기</output>

    <a href="/done">확인 화면</a>
    <a href="/done" target="_blank">새 창</a>

    <script>
        const value = localStorage.getItem('memo') || '';
        document.querySelector('textarea').value = value;
        document.querySelector('output').textContent = value || '대기';
    </script>
</body>
</html>
"""


class TestHandler(BaseHTTPRequestHandler):
    """이동 검증용 페이지를 제공한다."""

    def do_GET(self):
        """요청 경로에 검증용 화면을 반환한다."""
        body = PAGE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """요청 로그 출력을 생략한다."""
        return


def find_element(observation: dict, name: str) -> dict:
    """이름이 일치하는 요소 하나를 반환한다.

    Args:
        observation: 현재 관찰.
        name: 찾을 이름.

    Returns:
        관찰한 요소.

    Raises:
        AssertionError: 대상이 하나가 아닌 경우.
    """
    matches = [
        item for item in observation["elements"]
        if item["name"] == name
    ]

    assert len(matches) == 1, (
        f"요소를 하나로 확인하지 못했습니다: {name}"
    )

    return matches[0]


async def run_check(url: str) -> None:
    """입력·저장·링크 이동·새로고침을 검증한다.

    Args:
        url: 임시 서버의 시작 주소.

    Returns:
        없음.

    Raises:
        AssertionError: 검증 조건과 결과가 다른 경우.
    """
    artifact_dir = (
        Path("artifacts") / f"navigation-{uuid4().hex}"
    )

    async with PlaywrightSessions(targets={}) as sessions:
        session_id = await sessions.open(
            run_id="navigation-check",
            start_url=url,
        )

        tools = PlaywrightTools(sessions, artifact_dir)
        first = await tools.observe(session_id)

        page_target = find_element(first, "현재 페이지")
        assert page_target["actions"] == ["reload"]
        print("PASS: 현재 페이지의 새로고침 대상 관찰")

        new_window = find_element(first, "새 창")
        assert new_window["actions"] == []
        print("PASS: 지원하지 않는 새 창 링크 클릭 제외")

        try:
            BrowserAction(
                kind="reload",
                element_id=page_target["element_id"],
                value="잘못된 입력값",
            )
        except ValueError:
            pass
        else:
            raise AssertionError(
                "새로고침에 입력값을 허용했습니다."
            )

        print("PASS: 새로고침의 잘못된 입력값 거부")

        async def perform(
            name: str,
            kind: str,
            value: str | None = None,
        ) -> None:
            """현재 관찰의 대상에 검증된 행동을 실행한다."""
            current = tools.latest[session_id]
            target = find_element(current, name)

            decision = NextDecision.model_validate({
                "decision": "act",
                "description": "검증용 행동을 수행합니다.",
                "action": {
                    "kind": kind,
                    "element_id": target["element_id"],
                    "value": value,
                },
                "question": None,
            })

            validate_action_target(decision, current)

            action = decision.action.model_dump(
                exclude_none=True
            )
            action["observation_id"] = current["observation_id"]

            result = await tools.execute(session_id, action)
            assert result["success"], result

        await perform("메모", "fill", "리프로 검증")
        await perform("저장", "click")
        await perform("확인 화면", "click")

        current = tools.latest[session_id]
        assert urlsplit(current["url"]).path == "/done"
        print("PASS: 관찰한 링크 클릭과 주소 변경")

        before_reload_id = current["observation_id"]

        await perform("현재 페이지", "reload")

        final = tools.latest[session_id]

        assert final["observation_id"] != before_reload_id
        assert urlsplit(final["url"]).path == "/done"
        assert find_element(final, "저장 결과")["value"] == "리프로 검증"

        print("PASS: 새로고침 후 재관찰과 저장값 확인")

        try:
            await tools.execute(
                session_id,
                {
                    "observation_id": first["observation_id"],
                    "element_id": page_target["element_id"],
                    "kind": "reload",
                },
            )
        except ValueError:
            pass
        else:
            raise AssertionError(
                "오래된 관찰의 새로고침을 허용했습니다."
            )

        assert (artifact_dir / "events.jsonl").is_file()
        assert len(list(artifact_dir.glob("*.png"))) == 5

        print("PASS: 오래된 관찰 거부 및 실행 기록 저장")
        print("저장 위치:", artifact_dir)


def main() -> None:
    """임시 서버를 실행하고 검증 후 정리한다."""
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        TestHandler,
    )

    thread = Thread(
        target=server.serve_forever,
        daemon=True,
    )
    thread.start()

    try:
        asyncio.run(
            run_check(
                f"http://127.0.0.1:{server.server_port}/editor"
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()