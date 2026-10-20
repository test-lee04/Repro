"""제보와 화면 관찰을 바탕으로 실행 가능한 행동을 선택한다."""

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
    """관찰 도구가 허용한 행동인지 검사한다.

    Args:
        decision: 모델이 선택한 다음 결정.
        observation: 선택의 근거가 된 화면 관찰.

    Returns:
        없음.

    Raises:
        ValueError: 대상이 없거나 사용할 수 없는 행동인 경우.

    Notes:
        HTML 태그만으로 실행 가능 여부를 추측하지 않는다.
        실제 실행 직전의 대상 상태는 브라우저 도구가 다시 검사한다.
    """
    if decision.decision != "act":
        return

    action = decision.action

    if action is None:
        raise ValueError("행동 정보가 없습니다.")

    matches = [
        element
        for element in observation["elements"]
        if element["element_id"] == action.element_id
    ]

    if len(matches) != 1:
        raise ValueError(
            "행동 대상을 관찰에서 하나로 확인할 수 없습니다."
        )

    target = matches[0]

    if not target["enabled"]:
        raise ValueError(
            "비활성화된 요소에는 행동할 수 없습니다."
        )

    allowed_actions = target.get("actions", [])

    if action.kind not in allowed_actions:
        raise ValueError(
            "관찰 도구가 해당 요소에 허용하지 않은 행동입니다."
        )


class ActionPlanner:
    """모델의 행동 선택과 관찰 기반 검증을 연결한다."""

    def __init__(self, client: JsonModelClient):
        """행동 선택기를 준비한다.

        Args:
            client: 구조화 응답을 생성하는 모델 연결 객체.
        """
        self.client = client

    async def next_action(
        self,
        spec: BugSpec,
        observation: dict,
        history: list[dict],
    ) -> dict:
        """제보와 실제 실행 기록으로 다음 행동 하나를 선택한다.

        Args:
            spec: 구조화된 버그 제보.
            observation: 현재 화면의 관찰 결과.
            history: 실제 행동과 전후 화면 기록.

        Returns:
            검증된 결정, 모델 사용량, 소요 시간.

        Raises:
            ValueError: 선택한 행동이 관찰 정보와 맞지 않는 경우.
            pydantic.ValidationError: 모델 응답 형식이 잘못된 경우.

        Notes:
            행동을 실제로 실행하는 함수는 아니다.
            실행은 브라우저 도구가 담당한다.
        """
        schema = NextDecision.model_json_schema()

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

        payload = {
            "bug_spec": spec.model_dump(mode="json"),
            "observation": {
                "elements": observation["elements"],
                "truncated": observation.get(
                    "truncated",
                    False,
                ),
            },
            "history": executed_history,
            "history_note": (
                "기록은 실제로 실행한 행동이다. "
                "입력과 버튼 클릭은 별도 행동이다. "
                "실행 기록에 없는 행동을 수행했다고 가정하지 않는다."
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
                perf_counter() - started,
                2,
            ),
        }