"""검증할 화면 요소를 관찰하고 입력값과 표시 문구를 읽는다."""

import json
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from playwright.async_api import (
    ElementHandle,
    Error as PlaywrightError,
    Page,
)


class PageSource(Protocol):
    """기존 세션 관리자에서 페이지를 받기 위한 인터페이스."""

    def get_page(self, session_id: str) -> Page: ...


class VerificationBrowser:
    """현재 관찰에 포함된 요소에서 검증용 데이터를 읽는다."""

    def __init__(
        self,
        sessions: PageSource,
        artifact_dir: Path,
        max_elements: int = 500,
    ):
        """검증 도구를 준비한다.

        Args:
            sessions: get_page를 제공하는 기존 세션 관리자.
            artifact_dir: 관찰과 읽기 기록을 저장할 폴더.
            max_elements: 조사할 요소 수의 상한.

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
        self.handles: dict[
            str,
            dict[str, ElementHandle],
        ] = {}

    def _record(self, event: dict) -> dict:
        """관찰 또는 읽기 결과를 저장하고 반환한다."""
        path = self.artifact_dir / "verification_events.jsonl"

        with path.open("a", encoding="utf-8") as file:
            file.write(
                json.dumps(event, ensure_ascii=False)
                + "\n"
            )

        return event

    @staticmethod
    async def _describe(
        handle: ElementHandle,
    ) -> dict:
        """요소의 이름, 표시 문구와 현재 입력값을 읽는다.

        Args:
            handle: 실제 화면 요소를 가리키는 객체.

        Returns:
            요소의 연결 상태, 종류, 이름, 문구와 값.
        """
        return await handle.evaluate(
            """
            el => {
                const clean = value =>
                    (value || "").replace(/\\s+/g, " ").trim();

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
                const valueElement = [
                    "input", "textarea", "select"
                ].includes(tag);

                const text = clean(el.innerText);

                return {
                    connected: el.isConnected,
                    tag: tag,
                    name:
                        label ||
                        clean(el.getAttribute("placeholder")) ||
                        clean(el.getAttribute("title")) ||
                        text,
                    text: text,
                    value: valueElement ? el.value : null,
                    supports_value: valueElement
                };
            }
            """
        )

    async def _clear(
        self,
        session_id: str,
    ) -> None:
        """이전 관찰과 요소 연결을 정리한다."""
        self.latest.pop(session_id, None)

        handles = self.handles.pop(
            session_id,
            {},
        )

        for handle in handles.values():
            try:
                await handle.dispose()
            except PlaywrightError:
                pass

    async def observe(
        self,
        session_id: str,
    ) -> dict:
        """검증에 사용할 요소와 읽을 수 있는 데이터를 관찰한다.

        Args:
            session_id: 기존 세션 관리자의 세션 식별자.

        Returns:
            관찰 식별자, 주소, 요소 목록과 화면 기록.

        Notes:
            관찰 범위는 현재 페이지의 보이는 요소다.
            iframe 내부와 모든 사용자 정의 요소까지 포함하지는 않는다.
            새 관찰은 이전 검증용 관찰을 무효화한다.
        """
        await self._clear(session_id)

        page = self.sessions.get_page(session_id)
        observation_id = uuid4().hex

        candidates = await page.locator(
            "input, textarea, select, button, output, "
            "h1, h2, h3, h4, h5, h6, p, "
            "ul, ol, li, section, main, article, "
            "table, tr, td, th, div, span, [role]"
        ).element_handles()

        elements = []
        bindings = {}

        for index, handle in enumerate(candidates):
            if index >= self.max_elements:
                await handle.dispose()
                continue

            if not await handle.is_visible():
                await handle.dispose()
                continue

            info = await self._describe(handle)

            if not info["connected"]:
                await handle.dispose()
                continue

            element_id = (
                f"verification-{observation_id}-{index}"
            )

            elements.append({
                "element_id": element_id,
                **info,
            })

            bindings[element_id] = handle

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
            "candidate_count": len(candidates),
            "truncated": (
                len(candidates) > self.max_elements
            ),
            "screenshot": screenshot,
        }

        self.latest[session_id] = observation
        self.handles[session_id] = bindings

        return self._record({
            "type": "verification_observation",
            **observation,
        })

    async def read(
        self,
        session_id: str,
        observation_id: str,
        element_id: str,
        kind: str,
    ) -> dict:
        """현재 관찰의 요소에서 표시 문구 또는 입력값을 읽는다.

        Args:
            session_id: 검사할 세션.
            observation_id: 사용한 검증용 관찰 식별자.
            element_id: 현재 관찰에 있는 요소 식별자.
            kind: text 또는 value.

        Returns:
            ready이면 실제 값, 그 외에는 읽기를 보류한 이유.

        Notes:
            기대값과 비교하거나 버그 재현 여부를 판정하지 않는다.
            관찰 후 요소 내용이 바뀌면 다시 관찰하도록 요구한다.
        """
        result = {
            "type": "verification_read",
            "observation_id": observation_id,
            "element_id": element_id,
            "kind": kind,
            "status": None,
            "actual_value": None,
            "reason": None,
        }

        observation = self.latest.get(session_id)

        if (
            observation is None
            or observation["observation_id"] != observation_id
        ):
            result.update(
                status="stale",
                reason="최신 검증용 관찰이 필요합니다.",
            )
            return self._record(result)

        if kind not in {"text", "value"}:
            result.update(
                status="unsupported",
                reason=(
                    "이번 읽기 단계는 text와 value를 지원합니다."
                ),
            )
            return self._record(result)

        target = next(
            (
                element
                for element in observation["elements"]
                if element["element_id"] == element_id
            ),
            None,
        )

        if target is None:
            result.update(
                status="not_observed",
                reason="현재 관찰에 없는 대상입니다.",
            )
            return self._record(result)

        page = self.sessions.get_page(session_id)

        try:
            handle = self.handles[session_id][element_id]
            current = await self._describe(handle)
            visible = await handle.is_visible()

        except PlaywrightError:
            result.update(
                status="stale",
                reason="화면 요소를 다시 관찰해야 합니다.",
            )
            return self._record(result)

        changed = any(
            current[key] != target[key]
            for key in current
        )

        if (
            page.url != observation["url"]
            or not current["connected"]
            or not visible
            or changed
        ):
            result.update(
                status="stale",
                reason=(
                    "관찰 이후 화면이나 대상 정보가 변경됐습니다."
                ),
            )
            return self._record(result)

        if kind == "value" and not current["supports_value"]:
            result.update(
                status="incompatible",
                reason="입력값을 제공하는 요소가 아닙니다.",
            )
            return self._record(result)

        if kind == "text" and current["supports_value"]:
            result.update(
                status="incompatible",
                reason=(
                    "입력 요소의 내용은 value로 읽어야 합니다."
                ),
            )
            return self._record(result)

        result.update(
            status="ready",
            actual_value=current[kind],
        )

        return self._record(result)

    async def read_named(
        self,
        session_id: str,
        observation_id: str,
        name: str,
        kind: str,
    ) -> dict:
        """이름이 정확히 일치하는 대상 하나만 읽는다.

        Args:
            session_id: 검사할 세션.
            observation_id: 검증용 관찰 식별자.
            name: 현재 관찰에 기록된 정확한 요소 이름.
            kind: text 또는 value.

        Returns:
            대상이 하나면 읽기 결과, 없거나 여러 개면 보류 결과.

        Notes:
            이름의 정확한 일치만 검사하는 보수적인 연결 기능이다.
            자연어 대상 설명의 의미 해석은 후속 단계에서 연결한다.
        """
        observation = self.latest.get(session_id)

        if (
            observation is None
            or observation["observation_id"] != observation_id
        ):
            return self._record({
                "type": "verification_binding",
                "status": "stale",
                "actual_value": None,
                "reason": "최신 검증용 관찰이 필요합니다.",
            })

        matches = [
            element
            for element in observation["elements"]
            if element["name"] == name
        ]

        if len(matches) != 1:
            return self._record({
                "type": "verification_binding",
                "status": (
                    "ambiguous"
                    if len(matches) > 1
                    else "not_observed"
                ),
                "actual_value": None,
                "candidate_count": len(matches),
                "reason": (
                    "대상을 하나로 확정할 수 없습니다."
                ),
            })

        return await self.read(
            session_id,
            observation_id,
            matches[0]["element_id"],
            kind,
        )