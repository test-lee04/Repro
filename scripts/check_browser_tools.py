"""고정된 행동으로 브라우저 도구를 검증한다. AI는 사용하지 않는다."""

import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from app.browser.session import PlaywrightSessions
from app.browser.tools import PlaywrightTools


def read_value(observation: dict, element_id: str) -> str:
    return next(
        element["value"]
        for element in observation["elements"]
        if element["element_id"] == element_id
    )


async def main(version: str):
    artifact_dir = (
        Path("artifacts")
        / f"tools-{version}-{uuid4().hex}"
    )

    async with PlaywrightSessions(version=version) as sessions:
        session_id = await sessions.open(
            run_id="check-tools",
            target_id="demo_cart",
        )

        tools = PlaywrightTools(sessions, artifact_dir)

        first_observation = await tools.observe(session_id)
        assert read_value(first_observation, "total") == "20000"

        print("PASS: 초기 화면 관찰")

        actions = [
            {
                "kind": "fill",
                "element_id": "coupon",
                "value": "SAVE10",
            },
            {
                "kind": "click",
                "element_id": "apply",
            },
            {
                "kind": "fill",
                "element_id": "quantity",
                "value": "3",
            },
        ]

        for action in actions:
            current = tools.latest[session_id]

            result = await tools.execute(
                session_id,
                {
                    **action,
                    "observation_id": current["observation_id"],
                },
            )

            assert result["success"], result

            print(
                f"PASS: {action['kind']} "
                f"{action['element_id']}"
            )

        final_observation = tools.latest[session_id]
        actual = read_value(final_observation, "total")

        # 이 검사는 두 데모의 알려진 동작을 확인한다.
        # 버그의 잘못된 값을 정상 요구사항으로 정의하는 것은 아니다.
        fixture_expected = (
            "18000" if version == "buggy" else "27000"
        )
        assert actual == fixture_expected, actual

        print(f"실제 합계: {actual}원")
        print("정상 요구사항에 따른 기대 합계: 27000원")

        # 이전 화면의 ID로 행동하면 거부해야 한다.
        try:
            await tools.execute(
                session_id,
                {
                    "kind": "click",
                    "element_id": "reset",
                    "observation_id":
                        first_observation["observation_id"],
                },
            )
        except ValueError:
            print("PASS: 오래된 관찰을 사용한 행동 거부")
        else:
            raise AssertionError(
                "오래된 관찰로 행동이 실행됐습니다."
            )

        screenshots = list(artifact_dir.glob("*.png"))

        assert len(screenshots) == 4
        assert (artifact_dir / "events.jsonl").is_file()

        print("PASS: 스크린샷 및 행동 기록 저장")
        print(f"증거 폴더: {artifact_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--version",
        choices=["buggy", "fixed"],
        default="buggy",
    )
    args = parser.parse_args()

    asyncio.run(main(args.version))