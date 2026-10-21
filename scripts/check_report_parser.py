"""임의 제보의 모델 해석과 추가 질문 흐름을 확인한다."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from app.agent.model_client import build_model_client
from app.agent.report_parser import ReportParser
from app.agent.report_schema import ClarificationAnswer


async def main(args: argparse.Namespace) -> None:
    """제보를 분석하고 선택적으로 추가 답변을 반영한다.

    Args:
        args: 제보, 요구사항, 대화 모드 설정.

    Returns:
        없음.

    Notes:
        브라우저를 조작하지 않는다.
        최초 분석 후 최대 세 번의 답변 회차를 허용한다.
        각 분석 결과를 로컬 산출물 폴더에 저장한다.
    """
    load_dotenv()

    parser = ReportParser(build_model_client())
    answers: list[ClarificationAnswer] = []

    artifact_dir = (
        Path("artifacts")
        / f"report-{uuid4().hex}"
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)

    for round_index in range(4):
        result = await parser.parse(
            report=args.report,
            public_spec=args.public_spec,
            answers=answers,
        )

        spec = result["spec"]

        print(f"\n분석 회차: {round_index + 1}")
        print(spec.model_dump_json(indent=2))
        print("추가 질문 필요:", spec.needs_clarification)
        gate = result.get("question_review") or {}
        print("관찰 단계로 이월한 후보 질문:", len(gate.get("deferred_questions", [])))
        print("진단 기록:", result.get("diagnostic_path"))
        print("모델 사용량:", result["usage"])
        print("소요 시간:", result["elapsed_seconds"], "초")

        record = {
            "report": args.report,
            "public_spec": args.public_spec,
            "answers": result["answers"],
            "spec": spec.model_dump(mode="json"),
            "question_review": result.get("question_review"),
            "usage": result["usage"],
            "elapsed_seconds": result["elapsed_seconds"],
        }

        output_path = (
            artifact_dir
            / f"round-{round_index + 1}.json"
        )

        output_path.write_text(
            json.dumps(
                record,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        if not spec.needs_clarification:
            print("현재 사용자 답변을 기다릴 질문이 없습니다. 실제 화면 확인은 아직 수행하지 않았습니다.")
            break

        if not args.interactive:
            print(
                "추가 질문이 생성됐습니다. "
                "대화 모드는 실행하지 않았습니다."
            )
            break

        if round_index == 3:
            print("추가 답변 회차 한도에 도달했습니다.")
            break

        for item in spec.missing_information:
            print("\n확인 질문:", item.question)

            answer = input(
                "답변을 입력하세요. 중단하려면 엔터: "
            ).strip()

            if not answer:
                print("답변 입력을 중단했습니다.")
                print("저장 위치:", artifact_dir)
                return

            answers.append(
                ClarificationAnswer(
                    question=item.question,
                    answer=answer,
                )
            )

    print("\n저장 위치:", artifact_dir)
    print("해석 종료는 버그 재현 완료를 의미하지 않습니다.")


if __name__ == "__main__":
    argument_parser = argparse.ArgumentParser()

    argument_parser.add_argument(
        "--report",
        required=True,
    )
    argument_parser.add_argument(
        "--public-spec",
        default="",
    )
    argument_parser.add_argument(
        "--interactive",
        action="store_true",
    )

    asyncio.run(
        main(argument_parser.parse_args())
    )