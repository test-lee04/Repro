"""실제 Ollama 연결과 제보 구조화를 확인한다."""

import argparse
import asyncio

from dotenv import load_dotenv

from app.agent.model_client import build_model_client
from app.agent.report_parser import ReportParser
from app.agent.report_schema import ExpectationSource


CASES = {
    "complete": {
        "report": (
            "SAVE10 쿠폰을 적용하고 수량을 "
            "2개에서 3개로 바꾸면 합계가 그대로예요."
        ),
        "public_spec": (
            "상품 단가는 10000원이다. "
            "SAVE10 쿠폰은 10% 할인한다. "
            "합계 = 단가 × 수량 × (쿠폰 적용 시 0.9). "
            "수량을 변경하면 합계를 다시 계산해야 한다."
        ),
    },
    "unclear": {
        "report": "결제 금액이 이상해요.",
        "public_spec": "",
    },
}


async def main(case_name: str):
    load_dotenv()

    parser = ReportParser(build_model_client())
    case = CASES[case_name]

    result = await parser.parse(
        report=case["report"],
        public_spec=case["public_spec"],
    )

    spec = result["spec"]

    print("해석 결과:")
    print(spec.model_dump_json(indent=2))

    print("\n추가 질문 필요:", spec.needs_clarification)
    print("모델 사용량:", result["usage"])
    print("소요 시간:", result["elapsed_seconds"], "초")

    if case_name == "complete":
        assert (
            spec.expectation_source
            == ExpectationSource.PUBLIC_SPEC
        ), "기대 동작은 공개 요구사항에서 가져와야 합니다."

        extracted_inputs = {
            item.name: item.value
            for item in spec.inputs
        }

        assert extracted_inputs.get("coupon") == "SAVE10", (
            "제보에 명시된 쿠폰 코드가 누락되거나 잘못됐습니다."
        )

        assert extracted_inputs.get("initial_quantity") == "2", (
            "변경 전 수량이 누락되거나 잘못됐습니다."
        )

        assert extracted_inputs.get("updated_quantity") == "3", (
            "변경 후 수량이 누락되거나 잘못됐습니다."
        )

    else:
        assert (
            spec.expectation_source
            == ExpectationSource.UNKNOWN
        ), "근거 없는 기대 동작을 만들면 안 됩니다."

        assert spec.needs_clarification, (
            "불명확한 제보에는 추가 질문이 필요합니다."
        )

    print("\nPASS: 실제 모델 연결 및 기본 조건 검증")
    print("주의: 해석의 의미 정확성은 출력 내용을 읽고 확인하세요.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--case",
        choices=list(CASES),
        default="complete",
    )
    args = parser.parse_args()

    asyncio.run(main(args.case))