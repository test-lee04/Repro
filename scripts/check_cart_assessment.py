"""저장된 장바구니 실행 기록으로 재현 판정을 검증한다."""

import argparse
import json
from copy import deepcopy
from pathlib import Path

from app.agent.cart_assessor import (
    CartScenario,
    assess_cart,
)


def read_result(folder: Path) -> dict:
    path = folder / "loop_result.json"

    return json.loads(
        path.read_text(encoding="utf-8")
    )


def main(buggy_folder: Path, fixed_folder: Path):
    scenario = CartScenario()

    buggy_result = read_result(buggy_folder)
    fixed_result = read_result(fixed_folder)

    buggy_verdict = assess_cart(buggy_result, scenario)
    fixed_verdict = assess_cart(fixed_result, scenario)

    print("첫 번째 실행 기록의 판정:")
    print(json.dumps(
        buggy_verdict,
        ensure_ascii=False,
        indent=2,
    ))

    print("\n두 번째 실행 기록의 판정:")
    print(json.dumps(
        fixed_verdict,
        ensure_ascii=False,
        indent=2,
    ))

    assert buggy_verdict["status"] == "reproduced"
    assert buggy_verdict["expected_total"] == 27000
    assert buggy_verdict["actual_total"] == 18000
    print("\nPASS: 합계 미갱신 현상 판정")

    assert fixed_verdict["status"] == "not_reproduced"
    assert fixed_verdict["actual_total"] == 27000
    print("PASS: 정상 갱신 결과를 재현 성공으로 오판하지 않음")

    # 실제 실행 기록을 복사해 판정기의 거부 조건을 검사한다.
    # 원본 파일은 변경하지 않는다.
    missing_action = deepcopy(buggy_result)
    missing_action["history"].pop(1)

    assert (
        assess_cart(missing_action, scenario)["status"]
        == "inconclusive"
    )
    print("PASS: 쿠폰 적용 행동 누락 시 판정 보류")

    interrupted = deepcopy(buggy_result)
    interrupted["loop_status"] = "time_limit"

    assert (
        assess_cart(interrupted, scenario)["status"]
        == "inconclusive"
    )
    print("PASS: 제한 시간 종료를 재현 성공으로 오판하지 않음")

    for folder, verdict in [
        (buggy_folder, buggy_verdict),
        (fixed_folder, fixed_verdict),
    ]:
        (folder / "assessment.json").write_text(
            json.dumps(
                verdict,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    print("판정 결과를 각 폴더의 assessment.json에 저장했습니다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--buggy-dir", type=Path, required=True)
    parser.add_argument("--fixed-dir", type=Path, required=True)
    args = parser.parse_args()

    main(args.buggy_dir, args.fixed_dir)