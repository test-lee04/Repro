"""관찰된 허용 행동과 AI 행동 검증의 연결을 검사한다."""

import json
import unittest

from app.agent.action_planner import (
    ActionPlanner,
    validate_action_target,
)
from app.agent.action_schema import NextDecision
from app.agent.report_schema import BugSpec


def make_observation(
    actions: list[str],
    enabled: bool = True,
) -> dict:
    """여러 줄 입력창의 검증용 관찰을 만든다.

    Args:
        actions: 도구가 허용한 행동 목록.
        enabled: 요소 활성화 여부.

    Returns:
        검증용 관찰 데이터.
    """
    return {
        "observation_id": "test-observation",
        "truncated": False,
        "elements": [
            {
                "element_id": "observed-editor",
                "tag": "textarea",
                "name": "내용",
                "value": "",
                "enabled": enabled,
                "actions": actions,
            }
        ],
    }


def make_decision(
    kind: str = "fill",
    element_id: str = "observed-editor",
) -> NextDecision:
    """검증용 행동 결정을 만든다.

    Args:
        kind: 수행할 행동.
        element_id: 대상 식별자.

    Returns:
        형식 검증을 통과한 행동 결정.
    """
    return NextDecision.model_validate({
        "decision": "act",
        "description": "내용을 입력합니다.",
        "action": {
            "kind": kind,
            "element_id": element_id,
            "value": "확인할 내용" if kind == "fill" else None,
        },
        "question": None,
    })


class StubClient:
    """고정된 응답을 반환하는 검증용 모델 대역."""

    def __init__(self):
        self.messages = []

    async def generate(
        self,
        messages: list[dict],
        schema: dict,
    ) -> dict:
        """요청을 보관하고 검증용 행동을 반환한다.

        Args:
            messages: 모델에 전달할 메시지.
            schema: 요청한 응답 형식.

        Returns:
            검증용 구조화 응답.
        """
        self.messages = messages

        return {
            "content": make_decision().model_dump_json(),
            "usage": {"backend": "검증용"},
        }


class ActionCapabilityTests(
    unittest.IsolatedAsyncioTestCase
):
    """허용된 행동을 기준으로 검증하는지 검사한다."""

    def test_textarea_fill_allowed(self):
        """도구가 허용한 여러 줄 입력창의 입력을 승인한다."""
        validate_action_target(
            make_decision(),
            make_observation(["fill"]),
        )

    def test_empty_actions_rejected(self):
        """허용 행동이 없는 요소의 조작을 거부한다."""
        with self.assertRaises(ValueError):
            validate_action_target(
                make_decision(),
                make_observation([]),
            )

    def test_unlisted_action_rejected(self):
        """대상이 허용하지 않은 종류의 행동을 거부한다."""
        with self.assertRaises(ValueError):
            validate_action_target(
                make_decision(kind="click"),
                make_observation(["fill"]),
            )

    def test_disabled_target_rejected(self):
        """비활성화 요소의 행동을 거부한다."""
        with self.assertRaises(ValueError):
            validate_action_target(
                make_decision(),
                make_observation(
                    ["fill"],
                    enabled=False,
                ),
            )

    def test_unknown_target_rejected(self):
        """현재 관찰에 없는 대상의 행동을 거부한다."""
        with self.assertRaises(ValueError):
            validate_action_target(
                make_decision(element_id="unknown"),
                make_observation(["fill"]),
            )

    async def test_planner_forwards_capabilities(self):
        """허용 행동을 모델 입력에 유지하고 응답을 검증한다."""
        client = StubClient()

        spec = BugSpec(
            summary="저장한 내용이 유지되지 않음",
            inputs=[
                {
                    "name": "내용",
                    "value": "확인할 내용",
                }
            ],
            expected_behavior="저장한 내용 유지",
            expectation_source="report",
            expectation_evidence="저장한 내용이 유지돼야 합니다.",
        )

        result = await ActionPlanner(client).next_action(
            spec=spec,
            observation=make_observation(["fill"]),
            history=[],
        )

        payload = json.loads(
            client.messages[1]["content"]
        )

        self.assertEqual(
            payload["observation"]["elements"][0]["actions"],
            ["fill"],
        )

        self.assertEqual(
            result["decision"].action.element_id,
            "observed-editor",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)