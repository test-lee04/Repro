"""제보와 현재 화면을 바탕으로 다음 행동 하나를 선택한다."""

import json
from time import perf_counter

from app.agent.action_schema import NextDecision
from app.agent.model_client import JsonModelClient
from app.agent.prompts import ACTION_PROMPT
from app.agent.report_schema import BugSpec


def validate_action_target(
    decision: NextDecision,
    observation: dict,
) -> None:
    """선택한 행동이 현재 관찰에서 실행 가능한지 검사한다."""
    if decision.decision != "act":
        return

    action = decision.action

    if action is None:
        raise ValueError("행동 정보가 없습니다.")

    target = next(
        (
            element
            for element in observation["elements"]
            if element["element_id"] == action.element_id
        ),
        None,
    )

    if target is None:
        raise ValueError(
            "AI가 현재 관찰에 없는 요소를 선택했습니다."
        )

    if not target["enabled"]:
        raise ValueError(
            "AI가 비활성화된 요소를 선택했습니다."
        )

    if action.kind == "click" and target["tag"] != "button":
        raise ValueError(
            "입력창이나 출력 요소를 클릭 대상으로 선택했습니다."
        )

    if action.kind == "fill" and target["tag"] != "input":
        raise ValueError(
            "입력창이 아닌 요소에 입력하려고 했습니다."
        )


class ActionPlanner:
    def __init__(self, client: JsonModelClient):
        self.client = client

    async def next_action(
        self,
        spec: BugSpec,
        observation: dict,
        history: list[dict],
    ) -> dict:
        schema = NextDecision.model_json_schema()

        # 모델이 작성한 설명이 아니라 실제 실행 결과를 전달한다.
        executed_history = [
            {
                "executed_action": item["action"],
                "execution_success": item["success"],
                "error_type": item["error_type"],
                "before_elements": item["before_elements"],
                "after_elements": item["after_elements"],
            }
            for item in history
        ]

        # 현재는 화면에서 읽은 요소 정보만 사용한다.
        # 스크린샷과 실제 접속 URL은 모델에 전달하지 않는다.
        payload = {
            "bug_spec": spec.model_dump(mode="json"),
            "observation": {
                "elements": observation["elements"],
            },
            "history": executed_history,
            "history_note": (
                "history는 실제 실행한 행동의 기록이다. "
                "fill은 입력창의 값만 변경하고 버튼을 클릭하지 않는다. "
                "쿠폰 코드 입력과 쿠폰 적용 버튼 클릭은 별도 행동이다. "
                "실행 기록에 없는 행동은 수행됐다고 가정하지 않는다."
            ),
            "output_schema": schema,
        }

        started = perf_counter()

        response = await self.client.generate(
            messages=[
                {
                    "role": "system",
                    "content": ACTION_PROMPT,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        payload,
                        ensure_ascii=False,
                    ),
                },
            ],
            schema=schema,
        )

        decision = NextDecision.model_validate_json(
            response["content"]
        )

        validate_action_target(
            decision=decision,
            observation=observation,
        )

        return {
            "decision": decision,
            "usage": response["usage"],
            "elapsed_seconds": round(
                perf_counter() - started, 2
            ),
        }