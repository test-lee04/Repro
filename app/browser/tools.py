"""화면 관찰, 요소 조작, 링크 이동과 새로고침을 제공한다."""

import json
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
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
    """관찰된 대상과 실제 실행 위치를 연결한다."""

    def __init__(
        self,
        sessions: PlaywrightSessions,
        artifact_dir: Path,
        max_elements: int = 200,
    ):
        """브라우저 도구를 준비한다.

        Args:
            sessions: 브라우저 세션 관리자.
            artifact_dir: 화면과 실행 기록의 저장 폴더.
            max_elements: 한 번에 조사할 화면 요소 수의 상한.

        Raises:
            ValueError: 요소 수 제한이 양수가 아닌 경우.
        """
        if max_elements < 1:
            raise ValueError("요소 수 제한은 양수여야 합니다.")

        self.sessions = sessions
        self.artifact_dir = Path(artifact_dir)
        self.artifact_dir.mkdir(
            parents=True,
            exist_ok=True,
        )
        self.max_elements = max_elements

        self.latest: dict[str, dict] = {}
        self.bindings: dict[str, dict[str, Locator]] = {}

    def _record(self, event: dict) -> None:
        """관찰과 행동을 줄 단위 JSON으로 저장한다.

        Args:
            event: 저장할 기록.

        Returns:
            없음.

        Raises:
            OSError: 기록을 저장하지 못한 경우.
        """
        with (self.artifact_dir / "events.jsonl").open(
            "a",
            encoding="utf-8",
        ) as file:
            file.write(
                json.dumps(event, ensure_ascii=False) + "\n"
            )

    @staticmethod
    async def _describe(locator: Locator) -> dict:
        """요소의 이름, 값, 상태와 링크 속성을 읽는다.

        Args:
            locator: 요소 하나를 가리키는 위치 정보.

        Returns:
            관찰한 요소 정보.
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

                return {
                    dom_id: el.id || "",
                    tag: tag,
                    label: label,
                    placeholder: placeholder,
                    input_type: tag === "input" ? el.type : "",
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
                    readonly: Boolean(el.readOnly),
                    href: tag === "a" ? el.href : "",
                    target: tag === "a" ? el.target : "",
                    download: el.hasAttribute("download")
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
                    "[id=" + json.dumps(info["dom_id"]) + "]"
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

        role = {
            "button": "button",
            "a": "link",
        }.get(info["tag"])

        if role and info["name"]:
            candidates.append(
                page.get_by_role(
                    role,
                    name=info["name"],
                    exact=True,
                )
            )

        for candidate in candidates:
            if await candidate.count() == 1:
                return candidate

        return None

    @staticmethod
    def _link_allowed(page_url: str, info: dict) -> bool:
        """현재 지원 범위의 링크인지 검사한다.

        Args:
            page_url: 현재 페이지 주소.
            info: 링크의 관찰 정보.

        Returns:
            같은 출처의 현재 탭 이동 링크이면 True.

        Notes:
            링크 주소의 사전 검사이며 전체 이동 정책은 아니다.
        """
        if info["download"]:
            return False

        if info["target"] not in {"", "_self"}:
            return False

        try:
            current = urlsplit(page_url)
            destination = urlsplit(info["href"])

            if destination.scheme not in {"http", "https"}:
                return False

            def origin(parsed):
                default_port = (
                    443 if parsed.scheme == "https" else 80
                )
                return (
                    parsed.scheme,
                    parsed.hostname,
                    parsed.port or default_port,
                )

            return origin(current) == origin(destination)

        except ValueError:
            return False

    async def observe(self, session_id: str) -> dict:
        """화면 요소와 현재 페이지 대상을 관찰한다.

        Args:
            session_id: 관찰할 세션.

        Returns:
            관찰 식별자, 현재 주소, 요소와 화면 기록.

        Notes:
            현재 페이지는 새로고침을 위한 가상 대상이다.
            새 관찰은 이전 관찰의 실행 연결을 무효화한다.
        """
        self.latest.pop(session_id, None)
        self.bindings.pop(session_id, None)

        page = self.sessions.get_page(session_id)
        observation_id = uuid4().hex
        page_id = f"page-{observation_id}"

        elements = [{
            "element_id": page_id,
            "dom_id": "",
            "tag": "page",
            "name": "현재 페이지",
            "value": page.url,
            "enabled": True,
            "actions": ["reload"],
        }]

        bindings = {}
        used_ids = {page_id}

        controls = page.locator(
            "input, textarea, button, a[href], output, "
            "[role='status'], [role='alert']"
        )

        total_candidates = await controls.count()

        for index in range(
            min(total_candidates, self.max_elements)
        ):
            control = controls.nth(index)

            if not await control.is_visible():
                continue

            info = await self._describe(control)
            binding = await self._find_binding(page, info)

            dom_id = info["dom_id"]
            unique_dom_id = False

            if dom_id:
                unique_dom_id = (
                    await page.locator(
                        "[id=" + json.dumps(dom_id) + "]"
                    ).count()
                    == 1
                )

            element_id = (
                dom_id
                if unique_dom_id
                else f"observed-{observation_id}-{index}"
            )

            if element_id in used_ids:
                element_id = f"observed-{uuid4().hex}"

            used_ids.add(element_id)
            actions = []

            if binding is not None and info["enabled"]:
                if info["tag"] == "button":
                    actions.append("click")

                if (
                    info["tag"] == "a"
                    and self._link_allowed(page.url, info)
                ):
                    actions.append("click")

                text_input = (
                    info["tag"] == "textarea"
                    or (
                        info["tag"] == "input"
                        and info["input_type"] in {
                            "text", "search", "email",
                            "password", "tel", "url", "number",
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
            path=str(self.artifact_dir / screenshot),
            full_page=True,
        )

        observation = {
            "observation_id": observation_id,
            "session_id": session_id,
            "url": page.url,
            "elements": elements,
            "screenshot": screenshot,
            "candidate_count": total_candidates,
            "truncated": total_candidates > self.max_elements,
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
        """허용된 클릭, 입력 또는 새로고침을 실행한다.

        Args:
            session_id: 실행할 세션.
            action: 관찰 식별자, 대상, 행동, 입력값.

        Returns:
            실행 결과와 전후 관찰 식별자.

        Raises:
            ValueError: 관찰이 오래됐거나 행동과 대상이 부적절한 경우.

        Notes:
            도구 오류는 기록하지만 무조건 재시도하지 않는다.
            실행 후 재관찰 자체가 실패하면 예외를 전달한다.
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
                if item["element_id"] == action.get("element_id")
            ),
            None,
        )

        kind = action.get("kind")

        if target is None or kind not in target["actions"]:
            raise ValueError(
                "관찰된 대상에서 지원하는 행동이 아닙니다."
            )

        if kind == "fill":
            if not isinstance(action.get("value"), str):
                raise ValueError("입력값은 문자열이어야 합니다.")

        elif action.get("value") is not None:
            raise ValueError(
                "클릭과 새로고침에는 입력값을 넣지 않습니다."
            )

        page = self.sessions.get_page(session_id)

        if page.url != before["url"]:
            self.latest.pop(session_id, None)
            self.bindings.pop(session_id, None)
            raise ValueError(
                "페이지 주소가 변경됐습니다. 다시 관찰해야 합니다."
            )

        locator = None

        if kind != "reload":
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
                    "대상이 변경됐습니다. 다시 관찰해야 합니다."
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
            if kind == "reload":
                await page.reload(
                    wait_until="domcontentloaded",
                    timeout=10000,
                )

            elif kind == "click":
                await locator.click()
                await page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=10000,
                )

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
            "before_observation_id": before["observation_id"],
            "after_observation_id": after["observation_id"],
            "before_url": before["url"],
            "after_url": after["url"],
        }

        self._record(result)

        return result