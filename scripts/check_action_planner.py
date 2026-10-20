"""행동 선택 검증. 선택한 행동을 실제 실행하지는 않는다."""

import asyncio
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from app.agent.action_planner import (
    ActionPlanner,
    validate_action_target,
)
from app.agent.action_schema import NextDecision
from app.agent.model_client import build_model_client
from app.agent.report_parser import ReportParser
from app.browser.session import PlaywrightSessions
from app.browser.tools import PlaywrightTools


def check_invalid_target():
    observation = {
        "elements": [
            {
                "element_id": "quantity",
                "tag": "input",
                "enabled": True,
            }
        ]
    }

    decision = NextDecision.model_validate({
        "decision": "act",
        "description": "존재하지 않는 버튼 선택",
        "action": {
            "kind": "click",
            "element_id": "unknown_button",
            "value": None,
        },
        "question": None,
    })

    try:
        validate_action_target(decision, observation)
    except ValueError:
        print("PASS: 관찰에 없는 행동 대상 거부")
    else:
        raise AssertionError(
            "존재하지 않는 요소를 허용했습니다."
        )


async def main():
    load_dotenv()
    check_invalid_target()

    client = build_model_client()

    parser = ReportParser(client)
    parsed = await parser.parse(
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
        raise RuntimeError(
            "제보 해석에서 추가 정보가 필요하다고 판단했습니다."
        )

    artifact_dir = (
        Path("artifacts")
        / f"action-plan-{uuid4().hex}"
    )

    async with PlaywrightSessions() as sessions:
        session_id = await sessions.open(
            run_id="check-action-plan",
            target_id="demo_cart",
        )

        tools = PlaywrightTools(
            sessions=sessions,
            artifact_dir=artifact_dir,
        )

        observation = await tools.observe(session_id)

        planner = ActionPlanner(client)

        result = await planner.next_action(
            spec=spec,
            observation=observation,
            history=[],
        )

        decision = result["decision"]

        print("\nAI가 선택한 다음 행동:")
        print(decision.model_dump_json(indent=2))
        print("모델 사용량:", result["usage"])
        print("행동 선택 시간:", result["elapsed_seconds"], "초")

        # 이 사례의 초기 상태에서는 먼저 쿠폰을 입력해야 한다.
        assert decision.decision == "act", (
            "현재 사례에서는 행동을 선택해야 합니다."
        )
        assert decision.action is not None
        assert decision.action.kind == "fill", (
            "쿠폰 입력 행동이 필요합니다."
        )
        assert decision.action.element_id == "coupon", (
            "쿠폰 입력창을 선택해야 합니다."
        )
        assert decision.action.value == "SAVE10", (
            "제보의 쿠폰 코드를 사용해야 합니다."
        )

        (artifact_dir / "decision.json").write_text(
            decision.model_dump_json(indent=2),
            encoding="utf-8",
        )

        print("\nPASS: 실제 화면 기반 첫 행동 선택")
        print("선택한 행동은 아직 실행하지 않았습니다.")
        print("저장 위치:", artifact_dir)


if __name__ == "__main__":
    asyncio.run(main())