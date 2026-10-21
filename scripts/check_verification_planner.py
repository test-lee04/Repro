"""제보별 검증 조건 생성과 문구 확인 질문 및 추가 답변 반영을 검사한다."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from app.agent.model_client import build_model_client
from app.agent.report_parser import ReportParser, expectation_context
from app.agent.report_schema import ClarificationAnswer
from app.agent.verification_planner import VerificationPlanner


def collect_answers(questions, read=input):
    """현재 확인 질문의 답변만 모으고 빈 입력이면 중단한다.

    Args:
        questions: 생성기가 반환한 현재 질문 목록.
        read: 콘솔 입력 함수 또는 검사 대역.

    Returns:
        실제 답변 목록과 사용자 중단 여부.
    """
    answers = []
    for question in questions:
        print("\n확인 질문:", question)
        answer = read("답변을 입력하세요. 중단하려면 엔터: ")
        if not answer.strip():
            return answers, True
        answers.append(ClarificationAnswer(question=question, answer=answer))
    return answers, False


async def main(report, public_spec, interactive=False, max_rounds=3, reparse_report=False):
    """같은 원래 제보와 실제 답변을 누적해 조건을 다시 생성한다.

    Args:
        report: 사용자의 원래 제보.
        public_spec: 사용자가 제공한 서비스 요구사항.
        interactive: 확인 질문에 콘솔에서 답할지 여부.
        max_rounds: 최초 분석과 답변 반영을 합한 최대 회차.
        reparse_report: 답변으로 초기 상태나 행동도 정정할 경우 전체 제보를 다시 해석할지 여부.

    Notes:
        후보 생성은 실제 화면 연결이나 버그 재현 완료를 뜻하지 않는다.
        질문 자체는 기대 동작의 원문 근거가 아니다.
    """
    if max_rounds < 1:
        raise ValueError("최대 회차는 양수여야 합니다.")
    load_dotenv()
    client = build_model_client()
    planner = VerificationPlanner(client)
    folder = Path("artifacts") / f"verification-plan-{uuid4().hex}"
    folder.mkdir(parents=True, exist_ok=False)
    answers = []
    retained = None
    for number in range(1, max_rounds + 1):
        parser = ReportParser(client, retained_context=retained)
        parsed = await parser.parse(report=report, public_spec=public_spec, answers=answers)
        spec = parsed["spec"]
        print(f"\n분석 회차: {number}")
        print("\n제보 해석 결과:")
        print(spec.model_dump_json(indent=2))
        if parsed.get("interpretation_mode") == "retained_for_expectation":
            print("기대 조건 확인 회차: 초기 조건과 행동은 이전 해석을 유지합니다.")
        result = await planner.plan(spec, report=report, public_spec=public_spec, answers=answers)
        plan = result["plan"]
        print("\n검증 조건 생성 결과:")
        print(plan.model_dump_json(indent=2))
        print("\n제보 해석 모델 사용량:", parsed["usage"])
        print("조건 생성 모델 사용량:", result["usage"])
        print("조건 생성 소요 시간:", round(result["elapsed_seconds"], 2), "초")
        saved = {"report": report, "public_spec": public_spec,
                 "answers": [item.model_dump(mode="json") for item in answers],
                 "bug_spec": spec.model_dump(mode="json"), "plan": plan.model_dump(mode="json"),
                 "parser_usage": parsed["usage"], "planner_usage": result["usage"],
                 "elapsed_seconds": result["elapsed_seconds"], "attempts": result["attempts"],
                 "clarification_details": result.get("clarification_details", []),
                 "clarification_scope": result.get("clarification_scope"),
                 "diagnostic_path": result.get("diagnostic_path"),
                 "interpretation_mode": parsed.get("interpretation_mode", "generated")}
        text = json.dumps(saved, ensure_ascii=False, indent=2)
        (folder / f"round-{number}.json").write_text(text, encoding="utf-8")
        (folder / "verification_plan.json").write_text(text, encoding="utf-8")
        if plan.status != "needs_information":
            break
        if not interactive:
            print("필요한 확인 질문을 저장했습니다. 답변을 반영하려면 --interactive로 실행하세요.")
            break
        if number == max_rounds:
            print("답변 반영 회차 한도에 도달했습니다.")
            break
        new_answers, stopped = collect_answers(plan.questions)
        if not reparse_report and result.get("clarification_scope") == "expectation":
            retained = expectation_context(spec, report, public_spec, answers)
        else:
            retained = None
        answers.extend(new_answers)
        (folder / "answer_history.json").write_text(
            json.dumps([item.model_dump(mode="json") for item in answers], ensure_ascii=False, indent=2),
            encoding="utf-8")
        if stopped:
            print("답변 입력을 중단했습니다. 조건 후보를 확정하지 않았습니다.")
            break
    print("\n저장 위치:", folder)
    print("조건 후보 생성은 실제 화면 검증이나 버그 재현 완료를 의미하지 않습니다.")


if __name__ == "__main__":
    argument_parser = argparse.ArgumentParser(description="제보에 맞는 검증 조건 후보를 생성합니다.")
    argument_parser.add_argument("--report", required=True, help="분석할 버그 제보")
    argument_parser.add_argument("--public-spec", default="", help="정상 기대 동작의 근거가 되는 요구사항")
    argument_parser.add_argument("--interactive", action="store_true", help="필요한 확인 질문에 답하고 다시 분석")
    argument_parser.add_argument("--reparse-report", action="store_true", help="답변으로 초기 조건이나 행동도 정정하므로 전체 해석을 다시 실행")
    argument_parser.add_argument("--max-rounds", type=int, default=3, help="최대 분석 회차")
    arguments = argument_parser.parse_args()
    asyncio.run(main(report=arguments.report, public_spec=arguments.public_spec,
                     interactive=arguments.interactive, max_rounds=arguments.max_rounds,
                     reparse_report=arguments.reparse_report))
