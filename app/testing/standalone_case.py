"""고정된 행동과 정상 기대 조건을 AI 호출 없이 재실행한다."""

import argparse
import asyncio
import json
import math
import re
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit
from uuid import uuid4


class ReplayError(RuntimeError):
    """대상 연결이나 실행 환경 문제로 검사를 끝내지 못한 경우."""


async def resolve(page, spec):
    """기록된 위치 후보들이 같은 요소 하나를 가리키는지 확인한다."""
    allowed_tags = {
        "input", "textarea", "select", "button", "a", "output", "div", "span", "p",
        "ul", "ol", "li", "table", "tr", "td", "th", "section", "main", "article",
        "h1", "h2", "h3", "h4", "h5", "h6",
    }
    tag = spec["tag"]
    if tag not in allowed_tags:
        raise ReplayError("지원하지 않는 대상 태그입니다.")
    combined = None
    for recipe in spec["recipes"]:
        method, value = recipe["by"], recipe["value"]
        if not isinstance(value, str) or not value:
            raise ReplayError("대상을 찾을 관찰 속성이 없습니다.")
        if method == "id":
            locator = page.locator("[id=" + json.dumps(value) + "]")
        elif method == "label":
            locator = page.get_by_label(value, exact=True)
        elif method == "placeholder":
            locator = page.get_by_placeholder(value, exact=True)
        elif method == "role":
            locator = page.get_by_role(recipe["role"], name=value, exact=True)
        elif method == "text":
            locator = page.get_by_text(value, exact=True)
        elif method == "title":
            locator = page.get_by_title(value, exact=True)
        else:
            raise ReplayError("지원하지 않는 위치 찾기 방법입니다.")
        locator = locator.and_(page.locator(tag))
        combined = locator if combined is None else combined.or_(locator)
    if combined is None:
        raise ReplayError("대상을 찾을 위치 자료가 없습니다.")
    # 후보 중 첫 번째를 선택하지 않는다. 서로 다른 대상이 나오면 보류한다.
    await combined.wait_for(state="visible", timeout=5000)
    if await combined.count() != 1:
        raise ReplayError("대상을 하나로 확정할 수 없습니다.")
    return combined


def origin(url):
    """링크 클릭의 사전 검사에 사용할 출처를 반환한다."""
    parsed = urlsplit(url)
    return (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))


async def perform(page, action):
    """허용된 입력, 클릭 또는 새로고침 명령 하나를 실행한다."""
    kind = action["kind"]
    if kind == "reload":
        await page.reload(wait_until="domcontentloaded")
        return
    if kind not in {"fill", "click"}:
        raise ReplayError("지원하지 않는 행동입니다.")
    locator = await resolve(page, action["locator"])
    tag = action["locator"]["tag"]
    if kind == "fill":
        if tag not in {"input", "textarea"} or not isinstance(action.get("value"), str):
            raise ReplayError("입력 명령의 대상이나 값이 잘못됐습니다.")
        await locator.fill(action["value"])
    else:
        if tag not in {"button", "a"} or action.get("value") is not None:
            raise ReplayError("클릭 명령의 대상이나 값이 잘못됐습니다.")
        if tag == "a":
            href = await locator.evaluate("el => el.href")
            target = await locator.get_attribute("target")
            if (urlsplit(href).scheme not in {"http", "https"} or origin(href) != origin(page.url)
                    or target not in {None, "", "_self"}
                    or await locator.get_attribute("download") is not None):
                raise ReplayError("현재 탭의 같은 출처 링크만 재실행합니다.")
        await locator.click()
        await page.wait_for_load_state("domcontentloaded")


async def check_assertion(page, assertion, timeout):
    """정상 조건을 기다리며 읽고, 읽기 실패와 조건 불충족을 구분한다."""
    condition = assertion["condition"]
    kind, operator, expected = condition["kind"], condition["operator"], condition["expected_value"]
    if kind not in {"text", "value"} or operator not in {"equals", "contains"} or not isinstance(expected, str):
        raise ReplayError("지원하지 않는 검증 조건입니다.")
    is_input = assertion["locator"]["tag"] in {"input", "textarea", "select"}
    if (kind == "value") != is_input:
        raise ReplayError("읽기 종류와 대상이 맞지 않습니다.")
    started = perf_counter()
    while True:
        locator = await resolve(page, assertion["locator"])
        actual = await locator.input_value() if kind == "value" else re.sub(
            r"\s+", " ", await locator.inner_text()
        ).strip()
        passed = actual == expected if operator == "equals" else expected in actual
        if passed or perf_counter() - started >= timeout:
            return {"condition_id": condition["condition_id"], "passed": passed,
                    "expected_value": expected, "actual_value": actual,
                    "kind": kind, "operator": operator}
        await asyncio.sleep(0.1)


async def execute(case, url, folder, result, assertion_timeout):
    """새 브라우저 환경에서 고정된 행동과 조건만 재실행한다."""
    from playwright.async_api import async_playwright

    if case.get("format_version") != 1 or not case.get("actions") or not case.get("assertions"):
        raise ReplayError("테스트 자료의 형식이나 내용이 올바르지 않습니다.")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(5000)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=10000)
            if response is not None and response.status >= 400:
                raise ReplayError(f"시작 페이지의 HTTP 오류: {response.status}")
            for number, action in enumerate(case["actions"], 1):
                record = {"number": number, "action": action, "success": False}
                result["actions"].append(record)
                await perform(page, action)
                record["success"] = True
                record["url"] = page.url
            for assertion in case["assertions"]:
                result["assertions"].append(await check_assertion(page, assertion, assertion_timeout))
            await page.screenshot(path=str(folder / "final.png"), full_page=True)
            result["screenshot"] = "final.png"
            result["status"] = (
                "passed" if all(item["passed"] for item in result["assertions"])
                else "assertion_failed"
            )
        finally:
            await browser.close()


async def main(args):
    """실행 결과를 별도 폴더에 저장하고 종료 코드를 반환한다."""
    path = Path(args.case)
    folder = path.parent / f"replay-{uuid4().hex}"
    folder.mkdir(parents=True, exist_ok=True)
    result = {"status": "replay_error", "actions": [], "assertions": [],
              "case_path": str(path), "target_url": args.url, "error": None}
    started = perf_counter()
    try:
        if urlsplit(args.url).scheme not in {"http", "https"} or not urlsplit(args.url).hostname:
            raise ReplayError("HTTP 또는 HTTPS 대상 URL이 필요합니다.")
        case = json.loads(path.read_text(encoding="utf-8"))
        async with asyncio.timeout(args.timeout):
            await execute(case, args.url, folder, result, args.assert_timeout)
    except Exception as error:
        result["status"] = "replay_error"
        result["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        result["elapsed_seconds"] = round(perf_counter() - started, 2)
        (folder / "replay_result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print("재실행 결과:", result["status"])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("저장 위치:", folder)
    return {"passed": 0, "assertion_failed": 1, "replay_error": 2}[result["status"]]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default=str(Path(__file__).with_name("case.json")))
    parser.add_argument("--url", required=True)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--assert-timeout", type=float, default=3)
    args = parser.parse_args()
    if any(not math.isfinite(value) or value <= 0 for value in [args.timeout, args.assert_timeout]):
        parser.error("제한 시간은 양수이고 유한해야 합니다.")
    raise SystemExit(asyncio.run(main(args)))
