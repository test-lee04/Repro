"""실제 확인 답변이 저장된 조건 생성 기록을 별도 평가 입력으로 검증한다."""

import hashlib
import json
from pathlib import Path

from app.agent.report_parser import ReportParser
from app.agent.report_schema import BugSpec, ClarificationAnswer
from app.agent.verification_planner import VerificationPlanner
from app.agent.verification_schema import VerificationPlan


def load_confirmation(path, case):
    """원래 제보가 같은 저장 기록에서 해석과 실제 답변만 가져온다.

    Args:
        path: verification_plan.json 파일 또는 그 파일이 있는 폴더.
        case: 이번에 평가할 자료. 정답은 Agent에게 전달하지 않는다.

    Returns:
        원래 제보, 해석, 답변과 입력 기록의 식별값.

    Raises:
        ValueError: 다른 제보, 미확정 조건 또는 실제 답변이 없는 기록인 경우.

    Notes:
        저장된 조건은 입력 검증에만 사용한다. 실제 실행에서는 조건을 다시 생성한다.
        이 기록이 사람이 직접 작성했는지까지 증명하는 기능은 아니다.
    """
    path = Path(path)
    if path.is_dir():
        path = path / "verification_plan.json"
    raw = path.read_bytes()
    saved = json.loads(raw)
    if case.needs_information:
        raise ValueError("정보 부족 질문용 평가 자료에는 이 답변 후 판정 평가를 적용하지 않습니다.")
    if saved.get("report") != case.report or saved.get("public_spec", ""):
        raise ValueError("저장된 원래 제보와 요구사항이 현재 평가 자료와 다릅니다.")
    plan = VerificationPlan.model_validate(saved.get("plan"))
    if plan.status != "candidate":
        raise ValueError("확정된 조건 후보 기록을 선택하세요. 질문 대기 기록으로 실행할 수 없습니다.")
    answers = [ClarificationAnswer.model_validate(item) for item in saved.get("answers", [])]
    if not answers:
        raise ValueError("실제 확인 답변이 저장된 기록이 필요합니다.")
    if not any(item.source.value == "user_answer" for item in plan.conditions):
        raise ValueError("실제 답변에 근거한 조건이 없는 기록입니다.")
    spec = BugSpec.model_validate(saved.get("bug_spec"))
    if spec.needs_clarification or not spec.reported_steps:
        raise ValueError("원래 제보 해석의 필수 질문이나 조작 순서를 먼저 확인하세요.")
    spec, _, _ = ReportParser._resolve_evidence(spec, case.report, "", answers)
    ReportParser._validate_preconditions(spec, report=case.report, answers=answers)
    VerificationPlanner._validate_targets(plan)
    VerificationPlanner._validate_evidence(plan, report=case.report, public_spec="", answers=answers)
    VerificationPlanner._validate_text_literals(plan)
    return {"path": str(path.resolve()), "fingerprint": hashlib.sha256(raw).hexdigest(),
            "report": case.report, "public_spec": "", "spec": spec,
            "answers": answers}
