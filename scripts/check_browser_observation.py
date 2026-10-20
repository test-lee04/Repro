"""HTML id가 없는 요소의 관찰과 조작을 검증한다."""

import asyncio
from http.server import (
    BaseHTTPRequestHandler,
    ThreadingHTTPServer,
)
from pathlib import Path
from threading import Thread
from uuid import uuid4

from app.browser.session import PlaywrightSessions
from app.browser.tools import PlaywrightTools


PAGE_HTML = """
<!doctype html>
<html lang="ko">
<meta charset="utf-8">
<title>관찰 도구 검증</title>
<body>
    <label>제목 <input type="text"></label>
    <label>내용 <textarea></textarea></label>

    <button onclick="
        document.querySelector('output').textContent =
        document.querySelector('input').value;
    ">저장</button>

    <output aria-label="저장 결과">대기</output>

    <button>옵션</button>
    <button>옵션</button>
    <button disabled>취소</button>
    <input hidden aria-label="숨긴 입력">
</body>
</html>
"""


class TestHandler(BaseHTTPRequestHandler):
    """검증용 페이지를 제공한다."""

    def do_GET(self):
        """요청에 검증용 화면을 반환한다."""
        body = PAGE_HTML.encode("utf-8")

        self.send_response(200)
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
        """요청 로그 출력을 생략한다."""
        return


def find_element(
    observation: dict,
    name: str,
) -> dict:
    """이름이 일치하는 관찰 요소 하나를 반환한다.

    Args:
        observation: 화면 관찰 결과.
        name: 찾을 요소의 이름.

    Returns:
        일치하는 요소 정보.

    Raises:
        AssertionError: 일치하는 후보가 하나가 아닌 경우.
    """
    matches = [
        item
        for item in observation["elements"]
        if item["name"] == name
    ]

    assert len(matches) == 1, (
        f"요소를 하나로 확인하지 못했습니다: {name}"
    )

    return matches[0]


async def run_check(url: str) -> None:
    """ID 없는 요소 조작과 모호한 대상 거부를 확인한다.

    Args:
        url: 검증용 페이지 주소.

    Returns:
        없음.

    Raises:
        AssertionError: 검증 조건을 충족하지 못한 경우.
    """
    artifact_dir = (
        Path("artifacts")
        / f"observation-{uuid4().hex}"
    )

    async with PlaywrightSessions(
        targets={}
    ) as sessions:
        session_id = await sessions.open(
            run_id="observation-check",
            start_url=url,
        )

        tools = PlaywrightTools(
            sessions,
            artifact_dir,
        )

        observation = await tools.observe(
            session_id
        )

        first_observation_id = (
            observation["observation_id"]
        )

        assert all(
            not item["dom_id"]
            for item in observation["elements"]
        )

        assert not observation["truncated"]

        assert not any(
            item["name"] == "숨긴 입력"
            for item in observation["elements"]
        )

        print(
            "PASS: ID 없는 요소 관찰 및 숨긴 요소 제외"
        )

        options = [
            item
            for item in observation["elements"]
            if item["name"] == "옵션"
        ]

        assert len(options) == 2

        assert all(
            item["actions"] == []
            for item in options
        )

        try:
            await tools.execute(
                session_id,
                {
                    "observation_id": (
                        observation["observation_id"]
                    ),
                    "element_id": (
                        options[0]["element_id"]
                    ),
                    "kind": "click",
                },
            )

        except ValueError:
            pass

        else:
            raise AssertionError(
                "모호한 버튼에 클릭을 허용했습니다."
            )

        print(
            "PASS: 같은 이름의 모호한 버튼 조작 거부"
        )

        cancelled = find_element(
            observation,
            "취소",
        )

        assert not cancelled["enabled"]
        assert cancelled["actions"] == []

        print("PASS: 비활성화 요소 구분")

        for name, value in [
            ("제목", "새로운 기록"),
            ("내용", "관찰 도구 검증"),
        ]:
            current = tools.latest[session_id]

            target = find_element(
                current,
                name,
            )

            result = await tools.execute(
                session_id,
                {
                    "observation_id": (
                        current["observation_id"]
                    ),
                    "element_id": target["element_id"],
                    "kind": "fill",
                    "value": value,
                },
            )

            assert result["success"]

            after = find_element(
                tools.latest[session_id],
                name,
            )

            assert after["value"] == value

        print(
            "PASS: 라벨로 연결한 입력창과 여러 줄 입력창 조작"
        )

        current = tools.latest[session_id]

        save_button = find_element(
            current,
            "저장",
        )

        result = await tools.execute(
            session_id,
            {
                "observation_id": (
                    current["observation_id"]
                ),
                "element_id": (
                    save_button["element_id"]
                ),
                "kind": "click",
            },
        )

        assert result["success"]

        final = tools.latest[session_id]

        saved = find_element(
            final,
            "저장 결과",
        )

        assert saved["value"] == "새로운 기록"

        print(
            "PASS: 이름으로 연결한 버튼 클릭 및 결과 관찰"
        )

        try:
            await tools.execute(
                session_id,
                {
                    "observation_id": first_observation_id,
                    "element_id": (
                        save_button["element_id"]
                    ),
                    "kind": "click",
                },
            )

        except ValueError:
            pass

        else:
            raise AssertionError(
                "오래된 관찰을 허용했습니다."
            )

        print("PASS: 오래된 관찰 거부")

        screenshots = list(
            artifact_dir.glob("*.png")
        )

        assert len(screenshots) == 4

        assert (
            artifact_dir / "events.jsonl"
        ).is_file()

        print("PASS: 화면과 행동 기록 저장")
        print("저장 위치:", artifact_dir)


def main() -> None:
    """임시 서버를 시작하고 검증 후 정리한다.

    Returns:
        없음.

    Notes:
        외부 서비스와 모델을 호출하지 않는다.
        검증용 페이지는 실행 중에만 제공한다.
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

    try:
        asyncio.run(
            run_check(
                f"http://127.0.0.1:"
                f"{server.server_port}/"
            )
        )

    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()