"""자연어 제보에서 검증 조건 후보가 생성되는지 확인한다."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from app.agent.model_client import build_model_client
from app.agent.report_parser import ReportParser
from app.agent.verification_planner import VerificationPlanner


async def main(
    report: str,
    public_spec: str,
) -> None:
    """제보 해석과 검증 조건 생성을 실행하고 결과를 저장한다.

    Args:
        report: 사용자가 전달한 버그 제보.
        public_spec: 선택적으로 전달한 서비스 요구사항.
    """
    load_dotenv()

    client = build_model_client()
    parser = ReportParser(client)
    planner = VerificationPlanner(client)

    parsed = await parser.parse(
        report=report,
        public_spec=public_spec,
    )

    spec = parsed["spec"]

    print("\n제보 해석 결과:")
    print(
        json.dumps(
            spec.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
    )

    result = await planner.plan(
        spec,
        report=report,
        public_spec=public_spec,
    )

    plan = result["plan"]

    print("\n검증 조건 생성 결과:")
    print(
        json.dumps(
            plan.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
    )

    print("\n조건 생성 모델 사용량:", result["usage"])
    print(
        "조건 생성 소요 시간:",
        round(result["elapsed_seconds"], 2),
        "초",
    )

    folder = (
        Path("artifacts")
        / f"verification-plan-{uuid4().hex}"
    )
    folder.mkdir(parents=True, exist_ok=False)

    saved = {
        "report": report,
        "public_spec": public_spec,
        "bug_spec": spec.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
        "parser_usage": parsed["usage"],
        "planner_usage": result["usage"],
        "elapsed_seconds": result["elapsed_seconds"],
        "attempts": result["attempts"],
    }

    (folder / "verification_plan.json").write_text(
        json.dumps(
            saved,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("\n저장 위치:", folder)
    print(
        "조건 후보 생성은 실제 화면 검증이나 "
        "버그 재현 완료를 의미하지 않습니다."
    )


if __name__ == "__main__":
    argument_parser = argparse.ArgumentParser(
        description="제보에 맞는 검증 조건 후보를 생성합니다."
    )

    argument_parser.add_argument(
        "--report",
        required=True,
        help="분석할 버그 제보",
    )

    argument_parser.add_argument(
        "--public-spec",
        default="",
        help="기대 동작의 근거가 되는 서비스 요구사항",
    )

    arguments = argument_parser.parse_args()

    asyncio.run(
        main(
            report=arguments.report,
            public_spec=arguments.public_spec,
        )
    )