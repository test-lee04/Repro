"""동일한 회귀 테스트의 원래 환경 실패와 수정 환경 통과를 기록한다."""

import asyncio
import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import HttpUrl, TypeAdapter

from app.testing.compiler import RecordedTestCompiler, UnsupportedCompilation
from app.testing.standalone_case import execute


class ReplayRunner(Protocol):
    """기록 폴더, 대상 주소와 환경 역할을 받는 재실행 인터페이스."""

    async def run(self, run_dir: Path, url: str, *, purpose: str) -> dict: ...


def write_json(path: Path, value):
    """임시 파일을 교체해 JSON 한 파일을 완성된 내용으로 저장한다."""
    temporary = path.with_name(path.name + f".{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validation_state(records, fingerprint):
    """현재 테스트 지문에 해당하는 최신 원래·수정 환경 결과를 판정한다."""
    current = [record for record in records if record["artifact_fingerprint"] == fingerprint]
    original = next((item for item in reversed(current) if item["purpose"] == "original"), None)
    fix = next((item for item in reversed(current) if item["purpose"] == "fix"), None)
    last = current[-1] if current else None
    baseline = original is not None and original["status"] == "assertion_failed"
    fix_passed = fix is not None and fix["status"] == "passed"
    ordered = baseline and fix_passed and current.index(fix) > current.index(original)
    status, state = "generated", "baseline_missing"
    reason = "원래 환경의 조건 실패를 아직 확인하지 못했습니다."
    if last and last["status"] == "replay_error":
        status, state = "environment_error", "replay_error"
        reason = "최근 재실행에 실행 오류가 있어 검증 완료로 표시하지 않습니다."
    elif ordered:
        status, state = "validated", "validated"
        reason = "동일한 테스트의 원래 환경 조건 실패와 이후 수정 환경 통과를 확인했습니다."
    elif baseline:
        status = "awaiting_fix_validation"
        state = "fix_failed" if fix and fix["status"] == "assertion_failed" else "fix_pending"
        reason = "원래 환경의 조건 실패를 확인했습니다. 이후 수정 환경 통과가 필요합니다."
    return {
        "test_status": status, "validation_status": state,
        "replay_status": last["status"] if last else "not_run",
        "artifact_fingerprint": fingerprint,
        "original_failure_confirmed": baseline,
        "fix_pass_confirmed": fix_passed,
        "reason": reason,
        "last_replay_path": last["result_path"] if last else None,
    }


def validate_result(case, result):
    """성공 상태가 실제 행동·검증 기록과 일치하는지 검사한다."""
    if result["status"] not in {"passed", "assertion_failed"}:
        raise ValueError("완료한 재실행 상태를 확인할 수 없습니다.")
    actions = result["actions"]
    assertions = result["assertions"]
    if len(actions) != len(case["actions"]) or len(assertions) != len(case["assertions"]):
        raise ValueError("모든 행동과 조건의 재실행 기록이 필요합니다.")
    for actual, expected in zip(actions, case["actions"]):
        if actual.get("success") is not True or actual.get("action") != expected:
            raise ValueError("성공한 행동 기록과 생성한 테스트가 다릅니다.")
    flags = []
    for actual, expected in zip(assertions, case["assertions"]):
        condition = expected["condition"]
        for key in ["condition_id", "kind", "operator", "expected_value"]:
            if actual.get(key) != condition[key]:
                raise ValueError("검증 조건과 재실행 결과의 내용이 다릅니다.")
        value = actual.get("actual_value")
        if type(value) is not str:
            raise ValueError("실제로 읽은 문자열이 필요합니다.")
        passed = (value == condition["expected_value"] if condition["operator"] == "equals"
                  else condition["expected_value"] in value)
        if actual.get("passed") is not passed:
            raise ValueError("실제 값과 조건 통과 여부가 일치하지 않습니다.")
        flags.append(passed)
    expected_status = "passed" if all(flags) else "assertion_failed"
    if not flags or result["status"] != expected_status:
        raise ValueError("전체 재실행 상태가 개별 검증 결과와 다릅니다.")


class RecordedReplayRunner:
    """고정 실행 코드를 재사용하고 재실행 이력과 생성 상태를 갱신한다."""

    def __init__(self, timeout_seconds=60, assertion_timeout=3, executor=None):
        """실행 제한과 검사 대역을 주입한다. 기본 실행에는 AI를 사용하지 않는다."""
        if any(not math.isfinite(value) or value <= 0
               for value in [timeout_seconds, assertion_timeout]):
            raise ValueError("제한 시간은 양수이고 유한해야 합니다.")
        self.timeout_seconds = timeout_seconds
        self.assertion_timeout = assertion_timeout
        self.executor = executor if executor is not None else execute

    async def run(self, run_dir: Path, url: str, *, purpose: str) -> dict:
        """재실행을 수행하고 원래 실행 기록의 테스트 상태만 갱신한다.

        Args:
            run_dir: workflow_result.json과 regression 폴더가 있는 실행 폴더.
            url: 사용자가 지정한 실제 재실행 대상 주소.
            purpose: 원래 환경은 original, 수정 환경은 fix.

        Returns:
            이번 재실행 정보와 갱신된 테스트 검증 상태.

        Notes:
            원래 제보의 reproduction_status와 assessment는 변경하지 않는다.
            검증 결과는 지정한 환경과 이번 실행에 한정된다.
        """
        if purpose not in {"original", "fix"}:
            raise ValueError("환경 역할은 original 또는 fix여야 합니다.")
        target_url = str(TypeAdapter(HttpUrl).validate_python(url))
        root = Path(run_dir).resolve()
        if not root.is_dir():
            raise ValueError("기존 실행 폴더가 필요합니다.")
        lock = root / ".replay.lock"
        try:
            with lock.open("x", encoding="utf-8") as file:
                file.write(str(os.getpid()))
        except FileExistsError as error:
            raise RuntimeError("같은 실행 폴더의 다른 재실행이 진행 중입니다.") from error
        try:
            return await self._run_locked(root, target_url, purpose)
        finally:
            lock.unlink(missing_ok=True)

    async def _run_locked(self, root, url, purpose):
        """생성 자료를 확인하고 한 번의 재실행 및 상태 저장을 수행한다."""
        workflow_path = root / "workflow_result.json"
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        case_path = root / "regression" / "case.json"
        script_path = root / "regression" / "test_regression.py"
        case_bytes, script_bytes = case_path.read_bytes(), script_path.read_bytes()
        runtime_bytes = Path(__file__).with_name("standalone_case.py").read_bytes()
        if script_bytes != runtime_bytes:
            raise UnsupportedCompilation("생성한 실행 코드와 현재 고정 코드가 다릅니다. 테스트를 다시 생성해야 합니다.")
        case = json.loads(case_bytes)
        compiler = RecordedTestCompiler()
        expected = compiler.compile_run(workflow)
        if case.get("format_version") != 1:
            raise UnsupportedCompilation("지원하지 않는 테스트 자료 형식입니다.")
        for key in ["actions", "assertions"]:
            if case.get(key) != expected[key]:
                raise UnsupportedCompilation("원래 실행 기록에서 생성한 행동 또는 검증 조건과 다릅니다.")
        fingerprint = hashlib.sha256(
            hashlib.sha256(case_bytes).digest() + hashlib.sha256(script_bytes).digest()
        ).hexdigest()
        history_path = root / "replay_history.json"
        history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {"runs": []}
        folder = root / "regression" / f"replay-{uuid4().hex}"
        folder.mkdir()
        result = {
            "status": "replay_error", "actions": [], "assertions": [], "error": None,
            "purpose": purpose, "target_url": url, "case_path": str(case_path),
            "artifact_fingerprint": fingerprint,
            "started_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        }
        started = perf_counter()
        cancelled = False
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self.executor(case, url, folder, result, self.assertion_timeout)
                validate_result(case, result)
        except asyncio.CancelledError:
            cancelled = True
            result.update(status="replay_error", error={"type": "CancelledError", "message": "재실행이 취소됐습니다."})
        except Exception as error:
            result.update(status="replay_error", error={"type": type(error).__name__, "message": str(error)})
        result["elapsed_seconds"] = round(perf_counter() - started, 2)
        result_path = folder / "replay_result.json"
        write_json(result_path, result)
        entry = {
            "purpose": purpose, "status": result["status"], "target_url": url,
            "artifact_fingerprint": fingerprint, "result_path": str(result_path),
            "started_at": result["started_at"], "elapsed_seconds": result["elapsed_seconds"],
            "error": result["error"],
        }
        history["runs"].append(entry)
        generation = workflow["test_generation"]
        generation.update(validation_state(history["runs"], fingerprint))
        generation["replay_history_path"] = str(history_path)
        write_json(history_path, history)
        write_json(root / "test_generation.json", generation)
        write_json(workflow_path, workflow)
        if cancelled:
            raise asyncio.CancelledError()
        return {"replay": entry, "test_generation": generation}
