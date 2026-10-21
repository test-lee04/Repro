"""제보 해석부터 행동 실행, 검사 시점 확인, 실제 값 비교까지 연결한다."""

import asyncio
import json
import math
from pathlib import Path
from time import perf_counter

from pydantic import BaseModel

from app.testing.condition_comparator import compare_condition


def plain(value):
    """Convert nested model results into JSON-compatible data."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    return value


class ReproductionWorkflow:
    """Reuse existing components without fixture-specific runtime branches."""

    def __init__(
        self, parser, condition_planner, loop, checkpoint_reviewer,
        binder, verification_tools, timeout_seconds=300, max_conditions=5,
    ):
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or max_conditions < 1:
            raise ValueError("실행 제한값은 양수이고 유한해야 합니다.")
        self.parser = parser
        self.condition_planner = condition_planner
        self.loop = loop
        self.checkpoint_reviewer = checkpoint_reviewer
        self.binder = binder
        self.verification_tools = verification_tools
        self.timeout_seconds = timeout_seconds
        self.max_conditions = max_conditions

    async def run(
        self, *, report, session_id, artifact_dir, public_spec="", answers=None
    ) -> dict:
        """Run the first integrated workflow and persist its evidence.

        Args:
            report: Arbitrary user bug report.
            session_id: An already opened browser session.
            artifact_dir: Output directory for this run.
            public_spec: Optional supplied service specification.
            answers: Optional previous clarification answers.

        Returns:
            Workflow status and per-condition evidence. No final bug verdict.

        Notes:
            This version checks conditions only after all reported steps.
            Intermediate checkpoints and verification-driven replanning are pending.
        """
        folder = Path(artifact_dir)
        folder.mkdir(parents=True, exist_ok=True)
        started = perf_counter()
        result = {
            "workflow_status": "running", "verification_status": "inconclusive",
            "reproduction_status": "pending", "phase": "parse",
            "questions": [], "checks": [], "error": None,
        }

        try:
            async with asyncio.timeout(self.timeout_seconds):
                parsed = await self.parser.parse(report=report, public_spec=public_spec,
                                                 answers=answers)
                result["interpretation"] = plain(parsed)
                spec = parsed["spec"]
                result["phase"] = "conditions"
                planned = await self.condition_planner.plan(
                    spec, report=report, public_spec=public_spec, answers=answers
                )
                result["condition_generation"] = plain(planned)
                plan = planned["plan"]
                if plan.status != "candidate":
                    result["workflow_status"] = plan.status
                    result["questions"] = plan.questions
                    if plan.status == "unsupported":
                        result["verification_status"] = "unsupported"
                    return result
                if len(plan.conditions) > self.max_conditions:
                    result["workflow_status"] = "condition_limit"
                    return result

                result["phase"] = "actions"
                loop_result = await self.loop.run(spec, session_id, folder)
                result["execution"] = plain(loop_result)
                if loop_result["loop_status"] != "finish_selected":
                    result["workflow_status"] = loop_result["loop_status"]
                    if loop_result.get("question"):
                        result["questions"] = [loop_result["question"]]
                    return result

                for condition in plan.conditions:
                    check = {"condition": condition.model_dump(mode="json")}
                    result["checks"].append(check)
                    result["phase"] = "checkpoint"
                    reviewed = await self.checkpoint_reviewer.review(spec, condition, loop_result)
                    check["checkpoint"] = plain(reviewed)
                    reached = reviewed["review"].status == "reached"
                    binding_result = None
                    read_result = None
                    if reached:
                        result["phase"] = "binding"
                        observation = await self.verification_tools.observe(session_id)
                        check["observation"] = plain(observation)
                        binding_result = await self.binder.bind(condition, observation)
                        check["binding_result"] = plain(binding_result)
                        binding = binding_result["binding"]
                        if binding.status == "bound":
                            result["phase"] = "read"
                            read_result = await self.verification_tools.read(
                                session_id, binding_result["observation_id"],
                                binding.element_id, condition.kind,
                            )
                            check["read_result"] = plain(read_result)
                    result["phase"] = "compare"
                    comparison = compare_condition(
                        condition, binding_result, read_result, checkpoint_reached=reached
                    )
                    check["comparison"] = comparison.model_dump(mode="json")

                statuses = [check["comparison"]["status"] for check in result["checks"]]
                if all(status == "unsupported" for status in statuses):
                    result["verification_status"] = "unsupported"
                elif any(status in {"inconclusive", "unsupported"} for status in statuses):
                    result["verification_status"] = "inconclusive"
                elif "unsatisfied" in statuses:
                    result["verification_status"] = "unsatisfied"
                else:
                    result["verification_status"] = "satisfied"
                result["workflow_status"] = "conditions_checked"
                result["phase"] = "done"

        except TimeoutError:
            result["workflow_status"] = "time_limit"
        except asyncio.CancelledError:
            result["workflow_status"] = "cancelled"
            raise
        except Exception as error:
            result["workflow_status"] = "error"
            result["error"] = {
                "type": type(error).__name__, "message": str(error),
                "attempts": plain(getattr(error, "attempts", [])),
            }
        finally:
            result["elapsed_seconds"] = round(perf_counter() - started, 2)
            (folder / "workflow_result.json").write_text(
                json.dumps(plain(result), ensure_ascii=False, indent=2), encoding="utf-8"
            )

        return result
