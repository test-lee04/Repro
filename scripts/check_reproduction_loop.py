"""실제 모델의 행동 선택·실행 반복을 검증한다."""

import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from app.agent.action_planner import ActionPlanner
from app.agent.model_client import build_model_client
from app.agent.report_parser import ReportParser
from app.agent.reproduction_loop import ReproductionLoop
from app.browser.session import PlaywrightSessions
from app.browser.tools import PlaywrightTools


def read_value(observation: dict, element_id: str) -> str:
    return next(
        element["value"]
        for element in observation["elements"]
        if element["element_id"] == element_id
    )


def check_reported_sequence(history: list[dict]) -> None:
    """제보에 따른 행동 순서를 검증한다."""
    expected = [
        ("fill", "coupon", "SAVE10"),
        ("click", "apply", None),
        ("fill", "quantity", "3"),
    ]

    position = 0

    for item in history:
        if not item["success"]:
            continue

        action = item["action"]
        actual = (
            action["kind"],
            action["element_id"],
            action.get("value"),
        )

        if actual == expected[position]:
            position += 1

            if position == len(expected):
                return

    raise AssertionError(
        "쿠폰 입력 → 적용 → 수량 변경 순서를 확인하지 못했습니다."
    )


async def main(version: str):
    load_dotenv()

    client = build_model_client()

    parsed = await ReportParser(client).parse(
        report=(
            "SAVE10 쿠폰을 적용하고 수량을 "
            "2개에서 3개로 바꾸면 합계가 그대로예요."
        ),
        public_spec=(
            "상품 단가는 10000원이다. "
            "SAVE10 쿠폰은 10% 할인한다. "
            "수량을 변경하면 합계를 다시 계산해야 한다."
        ),
    )

    spec = parsed["spec"]

    if spec.needs_clarification:
        print(spec.model_dump_json(indent=2))
        raise RuntimeError("제보의 추가 정보가 필요합니다.")

    artifact_dir = (
        Path("artifacts")
        / f"loop-{version}-{uuid4().hex}"
    )

    async with PlaywrightSessions(version=version) as sessions:
        session_id = await sessions.open(
            run_id="check-loop",
            target_id="demo_cart",
        )

        tools = PlaywrightTools(
            sessions=sessions,
            artifact_dir=artifact_dir,
        )

        loop = ReproductionLoop(
            planner=ActionPlanner(client),
            tools=tools,
        )

        result = await loop.run(
            spec=spec,
            session_id=session_id,
            artifact_dir=artifact_dir,
        )

        print("\n반복 종료 상태:", result["loop_status"])
        print("행동 시도 횟수:", result["action_attempts"])
        print("행동 선택 모델 호출:", result["model_calls"])
        print("반복 구간 시간:", result["elapsed_seconds"], "초")
        print("추가 질문:", result["question"])
        print("저장 위치:", artifact_dir)

        assert result["loop_status"] == "finish_selected", (
            "행동 종료 선택까지 도달하지 못했습니다. "
            "저장된 실행 기록을 확인하세요."
        )

        check_reported_sequence(result["history"])

        final = result["final_observation"]

        assert read_value(final, "quantity") == "3"

        actual_total = read_value(final, "total")

        # 검증 스크립트만 알고 있는 데모 버전별 기대 관찰값이다.
        # 이 값을 AI의 행동 선택 입력으로 제공하지 않는다.
        fixture_expected = (
            "18000" if version == "buggy" else "27000"
        )

        assert actual_total == fixture_expected, (
            f"예상 데모 결과와 다릅니다: {actual_total}"
        )

        print("최종 실제 합계:", actual_total, "원")
        print("정상 요구사항의 기대 합계: 27000원")
        print("PASS: AI 행동 선택·실행·재관찰 반복")
        print("버그 재현 자동 판정은 다음 단계에서 구현합니다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--version",
        choices=["buggy", "fixed"],
        default="buggy",
    )
    args = parser.parse_args()

    asyncio.run(main(args.version))