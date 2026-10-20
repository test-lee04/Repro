"""장바구니 합계 미갱신 사례의 실행 기록을 판정한다."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CartScenario:
    # 공개된 데모 요구사항과 이번 제보의 재현 조건이다.
    unit_price: int = 10000
    discount_percent: int = 10
    coupon: str = "SAVE10"
    initial_quantity: int = 2
    updated_quantity: int = 3

    def expected_total(self, quantity: int) -> int:
        return (
            self.unit_price
            * quantity
            * (100 - self.discount_percent)
            // 100
        )


def element_value(elements: list[dict], element_id: str) -> str:
    matches = [
        element
        for element in elements
        if element["element_id"] == element_id
    ]

    if len(matches) != 1:
        raise ValueError(
            f"요소를 하나로 확인할 수 없습니다: {element_id}"
        )

    return str(matches[0]["value"])


def assess_cart(
    result: dict,
    scenario: CartScenario,
) -> dict:
    """모델 설명이 아니라 실제 행동과 화면 값으로 판정한다."""

    def verdict(status: str, message: str, **details) -> dict:
        return {
            "status": status,
            "message": message,
            "rule": "cart_coupon_quantity_stale_total",
            **details,
        }

    loop_status = result.get("loop_status")

    if loop_status == "needs_information":
        return verdict(
            "needs_information",
            "추가 정보가 필요해 재현 여부를 판정하지 않았습니다.",
        )

    if loop_status in {"tool_error", "error"}:
        return verdict(
            "environment_error",
            "실행 오류로 재현 여부를 판정하지 못했습니다.",
        )

    if loop_status != "finish_selected":
        return verdict(
            "inconclusive",
            "행동이 정상 종료되지 않아 판정하지 않았습니다.",
        )

    history = result.get("history", [])

    # 초기 버전은 이 세 행동으로 끝난 실행만 지원한다.
    # 다른 경로를 억지로 성공 처리하지 않는다.
    expected_actions = [
        ("fill", "coupon", scenario.coupon),
        ("click", "apply", None),
        ("fill", "quantity", str(scenario.updated_quantity)),
    ]

    actual_actions = [
        (
            item["action"]["kind"],
            item["action"]["element_id"],
            item["action"].get("value"),
        )
        for item in history
    ]

    if actual_actions != expected_actions:
        return verdict(
            "inconclusive",
            "지원하는 재현 순서와 실행 기록이 다릅니다.",
        )

    if any(not item["success"] for item in history):
        return verdict(
            "environment_error",
            "실패한 브라우저 행동이 포함돼 있습니다.",
        )

    try:
        coupon_input, coupon_apply, quantity_change = history

        initial_quantity = element_value(
            coupon_input["before_elements"], "quantity"
        )

        if initial_quantity != str(scenario.initial_quantity):
            return verdict(
                "inconclusive",
                "제보의 변경 전 수량과 초기 화면이 다릅니다.",
            )

        entered_coupon = element_value(
            coupon_apply["before_elements"], "coupon"
        )

        applied_message = element_value(
            coupon_apply["after_elements"], "message"
        )

        if (
            entered_coupon != scenario.coupon
            or applied_message != "10% 쿠폰 적용 완료"
        ):
            return verdict(
                "inconclusive",
                "쿠폰 적용을 화면에서 확인하지 못했습니다.",
            )

        before_quantity = element_value(
            quantity_change["before_elements"], "quantity"
        )
        after_quantity = element_value(
            quantity_change["after_elements"], "quantity"
        )

        if (
            before_quantity != str(scenario.initial_quantity)
            or after_quantity != str(scenario.updated_quantity)
        ):
            return verdict(
                "inconclusive",
                "수량 변경 전후 상태가 제보 조건과 다릅니다.",
            )

        before_total = int(element_value(
            quantity_change["before_elements"], "total"
        ))
        actual_total = int(element_value(
            quantity_change["after_elements"], "total"
        ))

        final = result["final_observation"]

        if (
            int(element_value(final["elements"], "total"))
            != actual_total
            or element_value(final["elements"], "quantity")
            != str(scenario.updated_quantity)
        ):
            return verdict(
                "inconclusive",
                "마지막 행동 결과와 최종 관찰이 일치하지 않습니다.",
            )

        expected_before = scenario.expected_total(
            scenario.initial_quantity
        )
        expected_after = scenario.expected_total(
            scenario.updated_quantity
        )

        if before_total != expected_before:
            return verdict(
                "inconclusive",
                "수량 변경 전 합계가 정상 조건과 달라 판정을 보류합니다.",
            )

        evidence = {
            "expected_total": expected_after,
            "actual_total": actual_total,
            "before_total": before_total,
            "final_observation_id": final["observation_id"],
            "screenshot": final["screenshot"],
        }

        if actual_total == expected_after:
            return verdict(
                "not_reproduced",
                "이번 실행에서는 합계가 정상 갱신돼 "
                "제보된 현상을 확인하지 못했습니다.",
                **evidence,
            )

        if actual_total == before_total:
            return verdict(
                "reproduced",
                "수량 변경 후에도 변경 전 합계가 유지되는 "
                "현상을 확인했습니다.",
                **evidence,
            )

        return verdict(
            "inconclusive",
            "합계가 정상값과 다르지만, 이번에 정의한 "
            "합계 미갱신 현상과도 일치하지 않습니다.",
            **evidence,
        )

    except (KeyError, TypeError, ValueError) as error:
        return verdict(
            "inconclusive",
            "필요한 화면 값이나 실행 기록을 읽지 못했습니다.",
            error_type=type(error).__name__,
        )