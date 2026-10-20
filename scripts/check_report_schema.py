"""제보 해석 데이터 형식과 잘못된 출력 거부를 검증한다."""

from copy import deepcopy

from pydantic import ValidationError

from app.agent.report_schema import BugSpec


def expect_invalid(payload: dict, label: str):
    try:
        BugSpec.model_validate(payload)
    except ValidationError:
        print(f"PASS: {label}")
    else:
        raise AssertionError(
            f"잘못된 데이터를 허용했습니다: {label}"
        )


def main():
    complete = {
        "summary": "쿠폰 적용 후 수량 변경 시 합계 미갱신",
        "reported_steps": [
            "SAVE10 쿠폰 적용",
            "수량을 2개에서 3개로 변경",
        ],
        "inputs": [
            {"name": "coupon", "value": "SAVE10"},
            {"name": "initial_quantity", "value": "2"},
            {"name": "updated_quantity", "value": "3"},
        ],
        "actual_behavior": "변경 전 합계가 유지됨",
        "expected_behavior": "변경한 수량에 맞춰 합계 갱신",
        "expectation_source": "public_spec",
        "expectation_evidence": (
            "합계 = 단가 × 수량 × "
            "(쿠폰 적용 시 0.9)"
        ),
        "missing_information": [],
    }

    spec = BugSpec.model_validate(complete)

    assert not spec.needs_clarification
    assert len(spec.inputs) == 3

    print("PASS: 정보가 충분한 해석 결과 검증")

    incomplete = {
        "summary": "결제 금액이 이상함",
        "actual_behavior": "금액이 이상하다는 제보",
        "expectation_source": "unknown",
        "missing_information": [
            {
                "field": "expected_behavior",
                "question": "기대한 금액이나 계산 방식을 알려주세요.",
            },
            {
                "field": "reported_steps",
                "question": "어떤 순서로 조작했는지 알려주세요.",
            },
        ],
    }

    spec = BugSpec.model_validate(incomplete)

    assert spec.needs_clarification

    print("PASS: 정보 부족 및 확인 질문 표현")

    invalid = deepcopy(incomplete)
    invalid["expected_behavior"] = "합계는 27000원이어야 함"

    expect_invalid(
        invalid,
        "출처 없는 기대 결과 거부",
    )

    invalid = deepcopy(complete)
    invalid.pop("expectation_evidence")

    expect_invalid(
        invalid,
        "근거 없는 기대 동작 거부",
    )

    invalid = deepcopy(complete)
    invalid["unexpected_field"] = "정의하지 않은 값"

    expect_invalid(
        invalid,
        "정의하지 않은 필드 거부",
    )

    # 다음 단계에서 사용할 JSON 출력 모양 확인
    print("\n구조화 결과 예시:")
    print(
        BugSpec.model_validate(complete).model_dump_json(
            indent=2
        )
    )


if __name__ == "__main__":
    main()