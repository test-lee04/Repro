"""데모 화면 관찰·조작 및 증거 저장 도구."""

import json
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from playwright.async_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeout,
)

from app.browser.session import PlaywrightSessions


class BrowserTools(Protocol):
    async def observe(self, session_id: str) -> dict: ...

    async def execute(self, session_id: str, action: dict) -> dict: ...


class PlaywrightTools:
    def __init__(
        self,
        sessions: PlaywrightSessions,
        artifact_dir: Path,
    ):
        self.sessions = sessions
        self.artifact_dir = Path(artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

        # 세션별 최신 관찰 정보
        self.latest: dict[str, dict] = {}

    def _record(self, event: dict) -> None:
        """이벤트를 한 줄에 하나씩 JSON으로 저장한다."""
        path = self.artifact_dir / "events.jsonl"

        with path.open("a", encoding="utf-8") as file:
            file.write(
                json.dumps(event, ensure_ascii=False) + "\n"
            )

    async def observe(self, session_id: str) -> dict:
        """화면에 표시된 입력창·버튼·결과를 수집한다."""
        page = self.sessions.get_page(session_id)
        elements = []

        controls = page.locator(
            "input, button, output, [role='status']"
        )

        for index in range(await controls.count()):
            control = controls.nth(index)

            if not await control.is_visible():
                continue

            # 현재 데모는 ID가 있는 요소만 대상으로 한다.
            element_id = await control.get_attribute("id")

            if not element_id:
                continue

            element = await control.evaluate(
                """
                el => ({
                    tag: el.tagName.toLowerCase(),
                    name:
                        el.getAttribute("aria-label") ||
                        (
                            el.labels && el.labels.length
                            ? el.labels[0].innerText
                            : el.innerText
                        ),
                    value:
                        el.tagName === "INPUT"
                        ? el.value
                        : el.innerText,
                    enabled: !el.disabled
                })
                """
            )

            elements.append({
                "element_id": element_id,
                **element,
            })

        observation_id = uuid4().hex
        screenshot = f"{observation_id}.png"

        await page.screenshot(
            path=str(self.artifact_dir / screenshot),
            full_page=True,
        )

        observation = {
            "observation_id": observation_id,
            "session_id": session_id,
            "elements": elements,
            "screenshot": screenshot,
        }

        self.latest[session_id] = observation

        self._record({
            "type": "observation",
            **observation,
        })

        return observation

    async def execute(
        self,
        session_id: str,
        action: dict,
    ) -> dict:
        """최신 관찰에 존재하는 요소에만 행동을 수행한다."""
        before = self.latest.get(session_id)

        if (
            before is None
            or action.get("observation_id")
            != before["observation_id"]
        ):
            raise ValueError(
                "최신 observation_id로 행동해야 합니다."
            )

        target = next(
            (
                element
                for element in before["elements"]
                if element["element_id"]
                == action.get("element_id")
            ),
            None,
        )

        if target is None or not target["enabled"]:
            raise ValueError(
                "관찰되지 않았거나 비활성화된 요소입니다."
            )

        kind = action.get("kind")

        if kind not in {"click", "fill"}:
            raise ValueError("click과 fill만 지원합니다.")

        if kind == "click" and target["tag"] != "button":
            raise ValueError("버튼만 클릭할 수 있습니다.")

        if kind == "fill":
            if (
                target["tag"] != "input"
                or not isinstance(action.get("value"), str)
            ):
                raise ValueError(
                    "입력창과 문자열 입력값이 필요합니다."
                )

        page = self.sessions.get_page(session_id)

        # 모델이 임의 selector를 전달하지 못하도록
        # 관찰된 ID를 이용해 locator를 만든다.
        selector = (
            "[id=" + json.dumps(target["element_id"]) + "]"
        )
        locator = page.locator(selector)

        # 한 번 사용한 관찰은 소비한다.
        # 시간 초과가 발생해도 같은 행동을 무조건 반복하지 않는다.
        del self.latest[session_id]

        error_type = None

        try:
            if kind == "click":
                await locator.click()
            else:
                await locator.fill(action["value"])

        except PlaywrightTimeout:
            error_type = "tool_timeout"

        except PlaywrightError:
            error_type = "browser_error"

        # 성공·실패 여부와 관계없이 현재 상태를 다시 확인한다.
        # 재관찰 자체가 불가능하면 오류를 호출자에게 전달한다.
        after = await self.observe(session_id)

        result = {
            "type": "action",
            "action": dict(action),
            "success": error_type is None,
            "error_type": error_type,
            "before_observation_id": before["observation_id"],
            "after_observation_id": after["observation_id"],
        }

        self._record(result)

        return result