"""AI의 행동 선택·실행·재관찰을 제한된 횟수와 시간 안에서 반복한다."""

import asyncio
import json
from pathlib import Path
from time import perf_counter

from app.agent.action_planner import ActionPlanner
from app.agent.report_schema import BugSpec
from app.browser.tools import PlaywrightTools


class ReproductionLoop:
    def __init__(
        self,
        planner: ActionPlanner,
        tools: PlaywrightTools,
        max_actions: int = 8,
        max_model_calls: int = 8,
        timeout_seconds: float = 120,
    ):
        if (
            max_actions < 1
            or max_model_calls < 1
            or timeout_seconds <= 0
        ):
            raise ValueError("실행 제한값은 양수여야 합니다.")

        self.planner = planner
        self.tools = tools
        self.max_actions = max_actions
        self.max_model_calls = max_model_calls
        self.timeout_seconds = timeout_seconds

    async def run(
        self,
        spec: BugSpec,
        session_id: str,
        artifact_dir: Path,
    ) -> dict:
        if spec.needs_clarification:
            raise ValueError(
                "추가 정보가 필요한 제보는 먼저 확인해야 합니다."
            )

        artifact_dir = Path(artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)

        started = perf_counter()

        result = {
            "loop_status": "running",
            "reproduction_status": "pending",
            "question": None,
            "decisions": [],
            "history": [],
            "model_calls": 0,
            "action_attempts": 0,
            "final_observation": None,
            "error": None,
        }

        try:
            async with asyncio.timeout(self.timeout_seconds):
                observation = await self.tools.observe(session_id)
                result["final_observation"] = observation

                while True:
                    if result["model_calls"] >= self.max_model_calls:
                        result["loop_status"] = "model_limit"
                        break

                    result["model_calls"] += 1

                    planned = await self.planner.next_action(
                        spec=spec,
                        observation=observation,
                        history=result["history"],
                    )

                    decision = planned["decision"]

                    result["decisions"].append({
                        "decision": decision.model_dump(mode="json"),
                        "usage": planned["usage"],
                        "elapsed_seconds": planned["elapsed_seconds"],
                        "completion_review": planned.get("completion_review"),
                    })

                    print(
                        f"[선택 {result['model_calls']}] "
                        f"{decision.description}",
                        flush=True,
                    )

                    if decision.decision == "finish":
                        result["loop_status"] = "finish_selected"
                        break

                    if decision.decision == "need_information":
                        result["loop_status"] = "needs_information"
                        result["question"] = decision.question
                        break

                    if result["action_attempts"] >= self.max_actions:
                        result["loop_status"] = "action_limit"
                        break

                    action = decision.action.model_dump(
                        exclude_none=True
                    )

                    # 최신 관찰 ID는 모델이 아니라 실행 코드가 붙인다.
                    action["observation_id"] = (
                        observation["observation_id"]
                    )

                    result["action_attempts"] += 1

                    executed = await self.tools.execute(
                        session_id=session_id,
                        action=action,
                    )

                    # 브라우저 도구가 행동 후 화면을 다시 관찰한다.
                    after = self.tools.latest[session_id]

                    result["history"].append({
                        "action": action,
                        "success": executed["success"],
                        "error_type": executed["error_type"],
                        "before_elements": observation["elements"],
                        "after_elements": after["elements"],
                    })

                    result["final_observation"] = after

                    print(
                        f"  실행: {action['kind']} "
                        f"{action['element_id']} "
                        f"/ 성공: {executed['success']}",
                        flush=True,
                    )

                    if not executed["success"]:
                        # 실패한 조작을 무조건 반복하지 않는다.
                        result["loop_status"] = "tool_error"
                        break

                    observation = after

        except TimeoutError:
            result["loop_status"] = "time_limit"

        except Exception as error:
            result["loop_status"] = "error"
            result["error"] = {
                "type": type(error).__name__,
                "message": str(error),
                "attempts": getattr(error, "attempts", []),
            }
            # 이미 수행한 행동과 최종 화면을 호출자에게 돌려줘 근거를 보존한다.

        finally:
            result["elapsed_seconds"] = round(
                perf_counter() - started, 2
            )

            (artifact_dir / "loop_result.json").write_text(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        return result