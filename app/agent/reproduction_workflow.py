"""제보 실행과 최종 재현 판정 후 회귀 테스트 파일을 자동 생성한다."""

import asyncio
import json
import math
from pathlib import Path
from time import perf_counter

from pydantic import BaseModel

from app.testing.condition_comparator import compare_condition
from app.testing.compiler import RecordedTestCompiler, UnsupportedCompilation
from app.testing.verdict import ReproductionStatus


def plain(value):
    """중첩된 모델 객체를 JSON으로 저장할 수 있는 자료로 바꾼다."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    return value


class ReproductionWorkflow:
    """기존 기능들을 재사용해 최종 판정까지 수행한다."""

    def __init__(
        self, parser, condition_planner, loop, checkpoint_reviewer,
        binder, verification_tools, timeout_seconds=300, max_conditions=5,
        *, assessor, test_compiler=None,
    ):
        """구성 요소와 최종 판정기를 주입하고 실행 제한을 설정한다."""
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or max_conditions < 1:
            raise ValueError("실행 제한값은 양수이고 유한해야 합니다.")
        if assessor is None:
            raise ValueError("최종 재현 판정기가 필요합니다.")
        self.parser = parser
        self.condition_planner = condition_planner
        self.loop = loop
        self.checkpoint_reviewer = checkpoint_reviewer
        self.binder = binder
        self.verification_tools = verification_tools
        self.assessor = assessor
        self.test_compiler = test_compiler if test_compiler is not None else RecordedTestCompiler()
        self.timeout_seconds = timeout_seconds
        self.max_conditions = max_conditions

    @staticmethod
    def _incomplete_assessment(result):
        """검사를 마치지 못한 실행에는 확정 재현 판정을 부여하지 않는다."""
        state = result["workflow_status"]
        status = ReproductionStatus.INCONCLUSIVE
        reason = "최종 재현 판정을 마치지 못했습니다."
        if state == "needs_information":
            status = ReproductionStatus.NEEDS_INFORMATION
            reason = "검사를 계속하려면 추가 답변이 필요합니다."
        elif state == "unsupported":
            status = ReproductionStatus.UNSUPPORTED
            reason = "현재 실행 방식에서 지원하지 않는 검사입니다."
        elif state == "tool_error":
            status = ReproductionStatus.ENVIRONMENT_ERROR
            reason = "브라우저 조작 오류로 검사를 완료하지 못했습니다."
        elif state in {"time_limit", "action_limit", "model_limit", "condition_limit"}:
            reason = f"실행 제한에 도달해 판정을 보류합니다: {state}"
        elif state == "cancelled":
            reason = "실행이 취소돼 판정을 보류합니다."
        return {"status": status.value, "reason": reason, "comparisons": [],
                "symptom_review": None, "attempts": []}

    async def run(
        self, *, report, session_id, artifact_dir, public_spec="", answers=None
    ) -> dict:
        """제보 실행부터 최종 판정과 회귀 테스트 생성을 수행한다.

        Args:
            report: 사용자가 입력한 버그 제보.
            session_id: 이미 열린 브라우저 세션의 식별자.
            artifact_dir: 이번 실행의 기록과 증거를 저장할 폴더.
            public_spec: 사용자가 제공한 서비스 요구사항.
            answers: 이전 확인 질문에 대한 답변.

        Returns:
            실행 상태, 최종 재현 판정, 테스트 생성 상태와 파일 경로.

        Notes:
            제한 시간에는 최종 판정 시간도 포함한다.
            중간 검사 시점과 검증 후 재계획은 아직 지원하지 않는다.
            취소는 기록을 저장한 뒤 호출자에게 다시 전달한다.
        """
        folder = Path(artifact_dir)
        folder.mkdir(parents=True, exist_ok=True)
        started = perf_counter()
        result = {
            "workflow_status": "running", "verification_status": "inconclusive",
            "reproduction_status": "pending", "assessment_status": "pending",
            "phase": "parse", "questions": [], "checks": [], "error": None,
            "test_generation": {
                "test_status": "not_generated", "generation_status": "not_started",
                "replay_status": "not_run", "reason": "테스트 생성을 시작하지 않았습니다.",
                "case_path": None, "test_path": None, "error": None,
            },
        }
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self._execute(
                    result, report=report, session_id=session_id, folder=folder,
                    public_spec=public_spec, answers=answers,
                )
                if result["workflow_status"] == "conditions_checked":
                    result["phase"] = "assessment"
                    assessment = plain(await self.assessor.assess(result))
                    status = ReproductionStatus(assessment["status"])
                    if status == ReproductionStatus.PENDING:
                        raise ValueError("최종 판정기가 pending을 반환할 수 없습니다.")
                    if not isinstance(assessment.get("reason"), str) or not assessment["reason"].strip():
                        raise ValueError("최종 판정 이유가 필요합니다.")
                    result["assessment"] = assessment
                    result["reproduction_status"] = status.value
                    result["assessment_status"] = "completed"

                else:
                    result["assessment"] = self._incomplete_assessment(result)
                    result["reproduction_status"] = result["assessment"]["status"]
                    result["assessment_status"] = "completed"

                result["phase"] = "test_generation"
                self._generate_test(result, folder)
                result["phase"] = "done"

        except TimeoutError:
            result["workflow_status"] = "time_limit"
            result["assessment_status"] = "not_completed"
        except asyncio.CancelledError:
            result["workflow_status"] = "cancelled"
            result["assessment_status"] = "not_completed"
            raise
        except Exception as error:
            result["workflow_status"] = "error"
            result["assessment_status"] = "not_completed"
            result["error"] = {
                "type": type(error).__name__, "message": str(error),
                "attempts": plain(getattr(error, "attempts", [])),
            }
        finally:
            if result["assessment_status"] != "completed":
                result["assessment"] = self._incomplete_assessment(result)
                result["reproduction_status"] = result["assessment"]["status"]
            if result["test_generation"]["generation_status"] == "not_started":
                result["test_generation"].update(
                    generation_status="skipped",
                    reason="최종 판정을 완료하지 못해 테스트 생성을 건너뛰었습니다.",
                )
            result["assessment"]["source_run"] = str(folder / "workflow_result.json")
            result["elapsed_seconds"] = round(perf_counter() - started, 2)
            (folder / "assessment.json").write_text(
                json.dumps(result["assessment"], ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (folder / "test_generation.json").write_text(
                json.dumps(result["test_generation"], ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (folder / "workflow_result.json").write_text(
                json.dumps(plain(result), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return result

    def _generate_test(self, result, folder):
        """판정과 분리해 테스트 생성 상태와 파일 위치를 기록한다."""
        generation = result["test_generation"]
        if result["reproduction_status"] not in {"reproduced", "not_reproduced"}:
            generation.update(
                generation_status="skipped",
                reason="재현 또는 미재현을 확정하지 못해 테스트를 생성하지 않습니다.",
            )
            return
        try:
            artifact = self.test_compiler.compile_run(result)
            artifact["source_run"] = str(folder / "workflow_result.json")
            exported = self.test_compiler.export(artifact, folder / "regression")
            if exported.get("test_status") != "generated":
                raise ValueError("생성기가 올바른 생성 완료 상태를 반환하지 않았습니다.")
            for key in ["case_path", "test_path"]:
                if not isinstance(exported.get(key), str) or not Path(exported[key]).is_file():
                    raise ValueError("생성된 테스트 파일을 확인할 수 없습니다.")
            generation.update(
                exported, generation_status="completed", replay_status="not_run",
                reason="회귀 테스트 파일을 생성했습니다. 재실행 검증은 아직 수행하지 않았습니다.",
            )
        except UnsupportedCompilation as error:
            generation.update(
                test_status="unsupported", generation_status="unsupported", reason=str(error),
            )
        except Exception as error:
            # 파일 생성의 실패를 이미 확인한 재현 판정의 실패로 바꾸지 않는다.
            generation.update(
                test_status="environment_error", generation_status="error",
                reason="회귀 테스트 파일 생성을 완료하지 못했습니다.",
                error={"type": type(error).__name__, "message": str(error)},
            )

    async def _execute(self, result, *, report, session_id, folder, public_spec, answers):
        """기존 해석, 행동 실행과 비교를 수행하고 중간 종료 이유를 유지한다."""
        parsed = await self.parser.parse(report=report, public_spec=public_spec, answers=answers)
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
            return
        if len(plan.conditions) > self.max_conditions:
            result["workflow_status"] = "condition_limit"
            return

        result["phase"] = "actions"
        loop_result = await self.loop.run(spec, session_id, folder)
        result["execution"] = plain(loop_result)
        if loop_result["loop_status"] != "finish_selected":
            result["workflow_status"] = loop_result["loop_status"]
            if loop_result.get("question"):
                result["questions"] = [loop_result["question"]]
            return

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
                        session_id, binding_result["observation_id"], binding.element_id, condition.kind
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
