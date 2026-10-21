"""실행 기록과 정상 검증 조건을 재사용 가능한 회귀 테스트로 변환한다."""

import json
from pathlib import Path
from typing import Literal, Protocol

from pydantic import ConfigDict, Field, StrictStr, model_validator

from app.agent.checkpoint_reviewer import CheckpointReview, CheckpointReviewer
from app.agent.report_schema import BugSpec, StrictModel
from app.agent.verification_schema import VerificationPlan
from app.testing.check_schema import VerificationCondition
from app.testing.condition_comparator import compare_condition


class UnsupportedCompilation(ValueError):
    """근거가 부족하거나 안정적인 실행 위치를 구성할 수 없는 경우."""


class LocatorRecipe(StrictModel):
    """관찰한 속성으로 만든 위치 찾기 자료."""

    model_config = ConfigDict(str_strip_whitespace=False)

    by: Literal["id", "label", "placeholder", "role", "text", "title"]
    value: StrictStr = Field(min_length=1)
    role: str | None = None


class LocatorSpec(StrictModel):
    """태그와 위치 찾기 방법을 묶은 대상 설명."""

    tag: Literal[
        "input", "textarea", "select", "button", "a", "output", "div", "span",
        "p", "ul", "ol", "li", "table", "tr", "td", "th", "section", "main",
        "article", "h1", "h2", "h3", "h4", "h5", "h6",
    ]
    recipes: list[LocatorRecipe] = Field(min_length=1)


class RecordedAction(StrictModel):
    """모델 코드를 실행하지 않는 제한된 재실행 명령."""

    model_config = ConfigDict(str_strip_whitespace=False)
    kind: Literal["fill", "click", "reload"]
    locator: LocatorSpec | None = None
    value: StrictStr | None = None

    @model_validator(mode="after")
    def validate_action(self):
        """행동에 필요한 위치와 입력값을 검사한다."""
        if self.kind == "reload":
            if self.locator is not None or self.value is not None:
                raise ValueError("새로고침에는 요소 위치와 입력값이 필요하지 않습니다.")
        elif self.locator is None:
            raise ValueError("입력과 클릭에는 관찰에서 만든 위치가 필요합니다.")
        elif self.kind == "fill":
            if self.value is None or self.locator.tag not in {"input", "textarea"}:
                raise ValueError("입력 명령의 대상이나 값이 올바르지 않습니다.")
        elif self.value is not None or self.locator.tag not in {"button", "a"}:
            raise ValueError("클릭 명령의 대상이나 값이 올바르지 않습니다.")
        return self


class RecordedAssertion(StrictModel):
    """정상 기대 조건과 그 조건을 읽을 위치."""

    condition: VerificationCondition
    locator: LocatorSpec

    @model_validator(mode="after")
    def validate_assertion(self):
        """현재 지원하는 문자열 읽기와 대상의 조합을 검사한다."""
        if self.condition.kind not in {"text", "value"}:
            raise ValueError("현재 테스트 생성은 text와 value를 지원합니다.")
        is_input = self.locator.tag in {"input", "textarea", "select"}
        if (self.condition.kind == "value") != is_input:
            raise ValueError("검증 조건과 대상 태그가 맞지 않습니다.")
        return self


class TestCompiler(Protocol):
    """기존 테스트 생성기의 공통 인터페이스."""

    def compile(self, actions: list[dict], assertions: list[dict]) -> dict: ...


def locator_from_observation(element: dict) -> dict:
    """관찰한 속성만 사용하며 임시 요소 식별자를 위치로 쓰지 않는다."""
    recipes = []
    for field, method in [("dom_id", "id"), ("label", "label"),
                          ("placeholder", "placeholder")]:
        if element.get(field):
            recipes.append({"by": method, "value": element[field]})

    name = element.get("name", "")
    tag = element.get("tag", "")
    if name:
        # 검증용 관찰은 이름의 출처를 분리하지 않으므로 후보 위치를 함께 기록한다.
        recipes.extend({"by": method, "value": name}
                       for method in ["label", "placeholder", "title"])
        role = {
            "button": "button", "a": "link", "input": "textbox",
            "textarea": "textbox", "select": "combobox", "ul": "list",
            "ol": "list", "li": "listitem", "table": "table", "tr": "row",
            "td": "cell", "th": "columnheader",
            **{f"h{n}": "heading" for n in range(1, 7)},
        }.get(tag)
        if role:
            recipes.append({"by": "role", "value": name, "role": role})
        if tag not in {"input", "textarea", "select"}:
            recipes.append({"by": "text", "value": name})

    unique = {json.dumps(recipe, sort_keys=True): recipe for recipe in recipes}
    if not unique:
        raise UnsupportedCompilation("대상을 다시 찾을 관찰 속성이 없습니다.")
    try:
        return LocatorSpec(tag=tag, recipes=list(unique.values())).model_dump(mode="json")
    except ValueError as error:
        raise UnsupportedCompilation(str(error)) from error


class RecordedTestCompiler:
    """기록을 제한된 데이터로 변환하고 고정 실행 코드를 내보낸다."""

    def compile(self, actions: list[dict], assertions: list[dict]) -> dict:
        """허용된 행동과 검증 조건으로 테스트 자료를 만든다."""
        if not actions or not assertions:
            raise UnsupportedCompilation("실행한 행동과 검증 조건이 모두 필요합니다.")
        try:
            validated_actions = [RecordedAction.model_validate(item) for item in actions]
            validated_assertions = [RecordedAssertion.model_validate(item) for item in assertions]
        except ValueError as error:
            raise UnsupportedCompilation(str(error)) from error
        return {
            "format_version": 1, "test_status": "generated",
            "actions": [item.model_dump(mode="json") for item in validated_actions],
            "assertions": [item.model_dump(mode="json") for item in validated_assertions],
        }

    def compile_run(self, run: dict) -> dict:
        """검증을 완료한 실행 기록에서 재실행 자료를 추출한다."""
        try:
            if (run["workflow_status"] != "conditions_checked"
                    or run["assessment_status"] != "completed"
                    or run["reproduction_status"] not in {"reproduced", "not_reproduced"}
                    or run["assessment"]["status"] != run["reproduction_status"]):
                raise UnsupportedCompilation("확인된 최종 재현 판정이 있는 실행이 필요합니다.")
            spec = BugSpec.model_validate(run["interpretation"]["spec"])
            plan = VerificationPlan.model_validate(run["condition_generation"]["plan"])
            execution = run["execution"]
            history = execution["history"]
            if plan.status != "candidate" or execution["loop_status"] != "finish_selected":
                raise UnsupportedCompilation("제보 행동 실행과 조건 생성을 완료해야 합니다.")
            actions = []
            for item in history:
                if item.get("success") is not True:
                    raise UnsupportedCompilation("실패한 행동이 있는 기록은 테스트로 확정하지 않습니다.")
                action = item["action"]
                targets = [element for element in item["before_elements"]
                           if element["element_id"] == action["element_id"]]
                if len(targets) != 1 or action["kind"] not in targets[0].get("actions", []):
                    raise UnsupportedCompilation("행동 대상을 원래 관찰에서 확인할 수 없습니다.")
                if action["kind"] == "reload":
                    if targets[0]["tag"] != "page" or action.get("value") is not None:
                        raise UnsupportedCompilation("현재 페이지의 새로고침 기록이 아닙니다.")
                    actions.append({"kind": "reload"})
                else:
                    actions.append({"kind": action["kind"], "value": action.get("value"),
                                    "locator": locator_from_observation(targets[0])})

            checks = {check["condition"]["condition_id"]: check for check in run["checks"]}
            ids = {condition.condition_id for condition in plan.conditions}
            if set(checks) != ids or len(checks) != len(run["checks"]):
                raise UnsupportedCompilation("생성한 조건과 검사 기록이 다릅니다.")
            assertions = []
            for condition in plan.conditions:
                check = checks[condition.condition_id]
                if check["condition"] != condition.model_dump(mode="json"):
                    raise UnsupportedCompilation("생성 이후 검증 조건이 변경됐습니다.")
                review = CheckpointReview.model_validate(check["checkpoint"]["review"])
                CheckpointReviewer.validate_references(review, spec.reported_steps, history)
                comparison = compare_condition(
                    condition, check.get("binding_result"), check.get("read_result"),
                    checkpoint_reached=review.status == "reached",
                )
                if comparison.status not in {"satisfied", "unsatisfied"}:
                    raise UnsupportedCompilation("실제 값 비교를 확정하지 못한 조건입니다.")
                observation = check["observation"]
                if observation["observation_id"] != comparison.observation_id:
                    raise UnsupportedCompilation("검사 관찰의 식별자가 다릅니다.")
                targets = [element for element in observation["elements"]
                           if element["element_id"] == comparison.element_id]
                if len(targets) != 1 or targets[0].get(condition.kind) != comparison.actual_value:
                    raise UnsupportedCompilation("검사 대상과 실제 읽은 값이 일치하지 않습니다.")
                assertions.append({"condition": condition.model_dump(mode="json"),
                                   "locator": locator_from_observation(targets[0])})
            artifact = self.compile(actions, assertions)
            artifact["source_reproduction_status"] = run["reproduction_status"]
            return artifact
        except UnsupportedCompilation:
            raise
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise UnsupportedCompilation(f"실행 기록을 변환할 수 없습니다: {error}") from error

    def export(self, artifact: dict, output_dir: Path) -> dict:
        """테스트 자료와 고정 Python 실행 파일을 저장한다."""
        folder = Path(output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        case_path = folder / "case.json"
        test_path = folder / "test_regression.py"
        case_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        test_path.write_text(Path(__file__).with_name("standalone_case.py").read_text(encoding="utf-8"),
                             encoding="utf-8")
        return {"test_status": "generated", "case_path": str(case_path),
                "test_path": str(test_path), "action_count": len(artifact["actions"]),
                "assertion_count": len(artifact["assertions"])}
