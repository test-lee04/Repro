"""화면 요소를 관찰하고 확인된 대상에 행동을 수행한다."""

import json
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from playwright.async_api import (
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeout,
)

from app.browser.session import PlaywrightSessions


class BrowserTools(Protocol):
    """브라우저 관찰과 행동 실행의 인터페이스."""

    async def observe(self, session_id: str) -> dict: ...

    async def execute(
        self,
        session_id: str,
        action: dict,
    ) -> dict: ...


class PlaywrightTools:
    """관찰 결과와 실제 브라우저 요소의 연결을 관리한다."""

    def __init__(
        self,
        sessions: PlaywrightSessions,
        artifact_dir: Path,
        max_elements: int = 200,
    ):
        """브라우저 도구를 준비한다.

        Args:
            sessions: 브라우저 세션 관리자.
            artifact_dir: 화면과 실행 기록을 저장할 폴더.
            max_elements: 한 번에 조사할 요소 수의 상한.

        Raises:
            ValueError: 요소 수 제한이 양수가 아닌 경우.
        """
        if max_elements < 1:
            raise ValueError(
                "요소 수 제한은 양수여야 합니다."
            )

        self.sessions = sessions
        self.artifact_dir = Path(artifact_dir)
        self.artifact_dir.mkdir(
            parents=True,
            exist_ok=True,
        )
        self.max_elements = max_elements

        self.latest: dict[str, dict] = {}

        # 실제 위치 정보는 모델에 전달하지 않는다.
        self.bindings: dict[
            str, dict[str, Locator]
        ] = {}

    def _record(self, event: dict) -> None:
        """관찰과 행동 기록을 줄 단위로 저장한다.

        Args:
            event: 저장할 이벤트.

        Returns:
            없음.

        Raises:
            OSError: 파일을 저장하지 못한 경우.
        """
        path = self.artifact_dir / "events.jsonl"

        with path.open(
            "a",
            encoding="utf-8",
        ) as file:
            file.write(
                json.dumps(
                    event,
                    ensure_ascii=False,
                )
                + "\n"
            )

    @staticmethod
    async def _describe(locator: Locator) -> dict:
        """요소의 이름, 값, 상태를 읽는다.

        Args:
            locator: 요소 하나를 가리키는 위치 정보.

        Returns:
            요소의 속성과 표시 정보.

        Notes:
            기본적인 라벨과 접근성 속성을 사용한다.
            모든 웹 요소의 이름 계산을 완전히 지원하지는 않는다.
        """
        return await locator.evaluate(
            """
            el => {
                const clean = text =>
                    (text || "").replace(/\\s+/g, " ").trim();

                const labelledBy = (
                    el.getAttribute("aria-labelledby") || ""
                ).split(/\\s+/).filter(Boolean).map(
                    id => document.getElementById(id)?.textContent || ""
                ).join(" ");

                const labels = el.labels
                    ? Array.from(el.labels).map(
                        label => label.innerText
                    ).join(" ")
                    : "";

                const label = clean(
                    el.getAttribute("aria-label") ||
                    labelledBy ||
                    labels
                );

                const tag = el.tagName.toLowerCase();
                const placeholder =
                    el.getAttribute("placeholder") || "";

                const inputType =
                    tag === "input" ? el.type : "";

                return {
                    dom_id: el.id || "",
                    tag: tag,
                    label: label,
                    placeholder: placeholder,
                    input_type: inputType,
                    name:
                        label ||
                        clean(placeholder) ||
                        clean(el.innerText) ||
                        clean(el.getAttribute("title")),
                    value:
                        (tag === "input" || tag === "textarea")
                        ? el.value
                        : el.innerText,
                    enabled:
                        !el.matches(":disabled") &&
                        el.getAttribute("aria-disabled") !== "true",
                    readonly: Boolean(el.readOnly)
                };
            }
            """
        )

    @staticmethod
    async def _find_binding(
        page: Page,
        info: dict,
    ) -> Locator | None:
        """요소를 하나로 구분할 수 있는 위치 정보를 찾는다.

        Args:
            page: 대상 페이지.
            info: 관찰한 요소 정보.

        Returns:
            하나의 요소를 가리키는 위치 정보 또는 None.
        """
        candidates = []

        if info["dom_id"]:
            candidates.append(
                page.locator(
                    "[id="
                    + json.dumps(info["dom_id"])
                    + "]"
                )
            )

        if info["tag"] in {"input", "textarea"}:
            if info["label"]:
                candidates.append(
                    page.get_by_label(
                        info["label"],
                        exact=True,
                    )
                )

            if info["placeholder"]:
                candidates.append(
                    page.get_by_placeholder(
                        info["placeholder"],
                        exact=True,
                    )
                )

        if info["tag"] == "button" and info["name"]:
            candidates.append(
                page.get_by_role(
                    "button",
                    name=info["name"],
                    exact=True,
                )
            )

        for candidate in candidates:
            if await candidate.count() == 1:
                return candidate

        return None

    async def observe(self, session_id: str) -> dict:
        """보이는 요소와 화면을 수집한다.

        Args:
            session_id: 관찰할 브라우저 세션.

        Returns:
            관찰 식별자, 요소 목록, 화면 파일과 조사 범위 정보.

        Notes:
            새 관찰을 시작하면 이전 관찰과 실행 연결을 무효화한다.
            현재는 기본 입력창, 버튼, 출력, 상태 문구를 조사한다.
        """
        self.latest.pop(session_id, None)
        self.bindings.pop(session_id, None)

        page = self.sessions.get_page(session_id)

        controls = page.locator(
            "input, textarea, button, output, "
            "[role='status'], [role='alert']"
        )

        total_candidates = await controls.count()
        observation_id = uuid4().hex

        elements = []
        bindings = {}
        used_ids = set()

        for index in range(
            min(total_candidates, self.max_elements)
        ):
            control = controls.nth(index)

            if not await control.is_visible():
                continue

            info = await self._describe(control)
            binding = await self._find_binding(
                page,
                info,
            )

            # 기존 식별자는 유지하되 HTML id를 필수로 요구하지 않는다.
            dom_id = info["dom_id"]
            unique_dom_id = False

            if dom_id:
                id_locator = page.locator(
                    "[id=" + json.dumps(dom_id) + "]"
                )

                unique_dom_id = (
                    await id_locator.count() == 1
                )

            element_id = (
                dom_id
                if unique_dom_id
                else f"observed-{observation_id}-{index}"
            )

            if element_id in used_ids:
                element_id = (
                    f"observed-{observation_id}-"
                    f"{index}-{uuid4().hex}"
                )

            used_ids.add(element_id)

            actions = []

            if binding is not None and info["enabled"]:
                if info["tag"] == "button":
                    actions.append("click")

                text_input = (
                    info["tag"] == "textarea"
                    or (
                        info["tag"] == "input"
                        and info["input_type"] in {
                            "text",
                            "search",
                            "email",
                            "password",
                            "tel",
                            "url",
                            "number",
                        }
                    )
                )

                if text_input and not info["readonly"]:
                    actions.append("fill")

            elements.append({
                "element_id": element_id,
                **info,
                "actions": actions,
            })

            if actions:
                bindings[element_id] = binding

        screenshot = f"{observation_id}.png"

        await page.screenshot(
            path=str(
                self.artifact_dir / screenshot
            ),
            full_page=True,
        )

        observation = {
            "observation_id": observation_id,
            "session_id": session_id,
            "elements": elements,
            "screenshot": screenshot,
            "candidate_count": total_candidates,
            "truncated": (
                total_candidates > self.max_elements
            ),
        }

        self.latest[session_id] = observation
        self.bindings[session_id] = bindings

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
        """최신 관찰에서 연결한 대상에 행동을 수행한다.

        Args:
            session_id: 실행할 세션.
            action: 관찰 식별자, 대상, 행동, 입력값.

        Returns:
            행동 성공 여부와 전후 관찰 식별자.

        Raises:
            ValueError: 오래된 관찰이거나 대상과 행동이 부적절한 경우.

        Notes:
            실행 직전 대상 정보를 다시 확인한다.
            확인과 실행 사이의 모든 화면 변화를 보장하지는 않는다.
        """
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
                item
                for item in before["elements"]
                if item["element_id"]
                == action.get("element_id")
            ),
            None,
        )

        kind = action.get("kind")

        if target is None or kind not in target["actions"]:
            raise ValueError(
                "관찰된 대상에서 지원하는 행동이 아닙니다."
            )

        if kind == "fill" and not isinstance(
            action.get("value"),
            str,
        ):
            raise ValueError(
                "입력값은 문자열이어야 합니다."
            )

        locator = self.bindings[session_id][
            target["element_id"]
        ]

        if (
            await locator.count() != 1
            or not await locator.is_visible()
        ):
            self.latest.pop(session_id, None)
            self.bindings.pop(session_id, None)

            raise ValueError(
                "대상 상태가 달라졌습니다. 다시 관찰해야 합니다."
            )

        current = await self._describe(locator)

        if any(
            current[key] != target[key]
            for key in current
        ):
            self.latest.pop(session_id, None)
            self.bindings.pop(session_id, None)

            raise ValueError(
                "대상 정보가 변경됐습니다. 다시 관찰해야 합니다."
            )

        self.latest.pop(session_id, None)
        self.bindings.pop(session_id, None)

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

        after = await self.observe(session_id)

        result = {
            "type": "action",
            "action": dict(action),
            "success": error_type is None,
            "error_type": error_type,
            "before_observation_id": (
                before["observation_id"]
            ),
            "after_observation_id": (
                after["observation_id"]
            ),
        }

        self._record(result)

        return result