"""Compare expected and observed values from the same observation."""

from typing import Literal

from pydantic import ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr

from app.agent.report_schema import StrictModel
from app.agent.target_binder import TargetBinding
from app.testing.check_schema import VerificationCondition


class ConditionComparison(StrictModel):
    """A condition result, separate from the overall reproduction verdict."""

    # 실제 읽은 문자열은 공백까지 보존해 기록한다.
    model_config = ConfigDict(str_strip_whitespace=False)

    condition_id: str = Field(min_length=1)
    status: Literal["satisfied", "unsatisfied", "inconclusive", "unsupported"]
    reason: str = Field(min_length=1)
    expected_value: StrictBool | StrictInt | StrictFloat | StrictStr
    actual_value: StrictStr | None = None
    observation_id: str | None = None
    element_id: str | None = None
    kind: str
    operator: str
    checkpoint_reached: bool


def compare_condition(
    condition: VerificationCondition,
    binding_result: dict | None,
    read_result: dict | None,
    *,
    checkpoint_reached: bool = False,
) -> ConditionComparison:
    """기록의 연결 관계를 확인하고 실제 값과 기대값을 비교한다.

    Args:
        condition: 원문 근거를 확인한 검증 조건 후보.
        binding_result: TargetBinder.bind가 반환한 연결 기록.
        read_result: VerificationBrowser.read가 반환한 실제 값 기록.
        checkpoint_reached: 실행 흐름에서 선행 행동과 검사 시점을 확인했는지 여부.

    Returns:
        조건 충족, 불충족, 판정 보류 또는 지원하지 않는 종류의 결과.

    Notes:
        checkpoint_reached는 호출하는 실행 흐름이 제공하는 확인 정보다.
        이 함수가 브라우저 행동 기록을 분석해 그 사실을 증명하지는 않는다.
        조건 불충족을 전체 버그 재현 성공으로 자동 변환하지 않는다.
    """
    base = {
        "condition_id": condition.condition_id,
        "expected_value": condition.expected_value,
        "kind": condition.kind,
        "operator": condition.operator,
        "checkpoint_reached": checkpoint_reached is True,
    }

    def result(status: str, reason: str, **details) -> ConditionComparison:
        return ConditionComparison(status=status, reason=reason, **base, **details)

    if condition.kind not in {"text", "value"}:
        return result("unsupported", "현재 실제 값 읽기는 text와 value만 지원합니다.")

    # 선행 행동과 검사 시점은 실행 흐름에서 확인해야 한다.
    # 값이 다르다는 이유만으로 검사 시점에 도달했다고 가정하지 않는다.
    if checkpoint_reached is not True:
        return result("inconclusive", "필요한 선행 행동과 검사 시점이 확인되지 않았습니다.")

    if not isinstance(binding_result, dict):
        return result("inconclusive", "대상 연결 기록이 없습니다.")
    if binding_result.get("condition_id") != condition.condition_id:
        return result("inconclusive", "다른 검증 조건의 연결 기록입니다.")

    try:
        binding = TargetBinding.model_validate(binding_result.get("binding"))
    except ValueError:
        return result("inconclusive", "대상 연결 기록의 형식이 올바르지 않습니다.")

    if binding.status == "unsupported":
        return result("unsupported", "현재 방식으로 검사 대상을 연결하지 못했습니다.")
    if binding.status != "bound":
        return result("inconclusive", f"대상을 확정하지 못했습니다: {binding.status}")

    observation_id = binding_result.get("observation_id")
    if not isinstance(observation_id, str) or not observation_id.strip():
        return result("inconclusive", "연결에 사용한 관찰 식별자가 없습니다.")

    if not isinstance(read_result, dict):
        return result("inconclusive", "실제 값을 읽은 기록이 없습니다.")

    if read_result.get("observation_id") != observation_id:
        return result("inconclusive", "연결과 읽기에 사용한 관찰이 다릅니다.")
    if read_result.get("element_id") != binding.element_id:
        return result("inconclusive", "연결한 대상과 값을 읽은 대상이 다릅니다.")
    if read_result.get("kind") != condition.kind:
        return result("inconclusive", "조건과 읽은 데이터의 종류가 다릅니다.")

    details = {"observation_id": observation_id, "element_id": binding.element_id}
    if read_result.get("status") != "ready":
        return result(
            "inconclusive",
            f"실제 값 읽기가 완료되지 않았습니다: {read_result.get('status')}",
            **details,
        )

    actual = read_result.get("actual_value")
    if type(actual) is not str:
        return result("inconclusive", "문자열 실제 값을 확인할 수 없습니다.", **details)

    # 빈 문자열도 실제로 읽은 값이다. 누락된 값인 None과 구별한다.
    # 입력값에 strip이나 공백 정리를 적용해 차이를 감추지 않는다.
    if condition.operator == "equals":
        satisfied = actual == condition.expected_value
    elif condition.operator == "contains":
        satisfied = condition.expected_value in actual
    else:
        return result("unsupported", "현재 문자열 비교에서 지원하지 않는 연산입니다.", **details)

    return result(
        "satisfied" if satisfied else "unsatisfied",
        "정상 기대 조건을 충족합니다." if satisfied else "정상 기대 조건을 충족하지 않습니다.",
        actual_value=actual,
        **details,
    )
