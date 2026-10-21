"""검증 대상 설명을 현재 관찰의 요소에 연결한다."""

import copy
import json
from pathlib import Path
from uuid import uuid4
import time
from typing import Literal

from pydantic import Field, model_validator

from app.agent.model_client import JsonModelClient
from app.agent.prompts import TARGET_BINDING_PROMPT
from app.agent.report_schema import StrictModel
from app.testing.check_schema import VerificationCondition


class TargetBinding(StrictModel):
    """대상 연결 결과이며 비교 결과나 재현 판정은 아니다."""

    status: Literal["bound", "ambiguous", "not_observed", "unsupported"]
    element_id: str | None = Field(default=None, min_length=1)
    candidate_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_binding(self):
        """상태와 대상 식별자의 일관성을 검사한다."""
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("후보 식별자는 중복될 수 없습니다.")

        if self.status == "bound":
            if self.element_id is None or self.candidate_ids != [self.element_id]:
                raise ValueError("연결 상태에는 선택한 대상 하나가 필요합니다.")
        elif self.status == "ambiguous":
            if self.element_id is not None or len(self.candidate_ids) < 2:
                raise ValueError("모호한 상태에는 둘 이상의 후보가 필요합니다.")
        elif self.element_id is not None or self.candidate_ids:
            raise ValueError("대상을 정하지 못한 상태에는 식별자를 넣지 않습니다.")

        return self


class TargetBinder:
    """모델의 대상 선택을 관찰 목록과 대조한다."""

    def __init__(
        self,
        client: JsonModelClient,
        max_attempts: int = 2,
        diagnostic_dir: Path | None = None,
    ):
        """모델과 첫 호출을 포함한 최대 시도 횟수를 설정한다."""
        if max_attempts < 1:
            raise ValueError("최대 시도 횟수는 양수여야 합니다.")
        self.client = client
        self.max_attempts = max_attempts
        self.diagnostic_dir = diagnostic_dir

    @staticmethod
    def _compatible(kind: str, element: dict) -> bool:
        """현재 읽기 도구에서 조건 종류와 요소가 맞는지 검사한다."""
        supports_value = element.get("supports_value") is True
        return (
            (kind == "value" and supports_value)
            or (kind == "text" and not supports_value)
        )

    @staticmethod
    def _generation_schema(candidate_ids: list[str]) -> dict:
        """상태별 필드 제약을 모델에 전달할 생성 형식에 반영한다."""
        base = TargetBinding.model_json_schema()
        branches = []
        statuses = ["bound", "not_observed", "unsupported"]
        if len(candidate_ids) >= 2:
            statuses.append("ambiguous")

        for status in statuses:
            branch = copy.deepcopy(base)
            branch["required"] = [
                "status", "element_id", "candidate_ids", "reason"
            ]
            properties = branch["properties"]
            properties["status"] = {"type": "string", "enum": [status]}
            properties["reason"] = {"type": "string", "minLength": 1}
            if status == "bound":
                properties["element_id"] = {
                    "type": "string", "enum": candidate_ids
                }
                properties["candidate_ids"] = {
                    "type": "array", "minItems": 1, "maxItems": 1,
                    "items": {"type": "string", "enum": candidate_ids},
                }
            elif status == "ambiguous":
                properties["element_id"] = {"type": "null"}
                properties["candidate_ids"] = {
                    "type": "array", "minItems": 2,
                    "uniqueItems": True,
                    "items": {"type": "string", "enum": candidate_ids},
                }
            else:
                properties["element_id"] = {"type": "null"}
                properties["candidate_ids"] = {
                    "type": "array", "maxItems": 0,
                    "items": {"type": "string"},
                }
            branches.append(branch)

        return {"anyOf": branches}

    def _save_failure(self, payload: dict, attempts: list[dict]) -> str | None:
        """모든 시도가 실패하면 원본 응답과 오류를 저장한다."""
        if self.diagnostic_dir is None:
            return None
        folder = Path(self.diagnostic_dir)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"binding-failure-{uuid4().hex}.json"
        path.write_text(
            json.dumps({"input": payload, "attempts": attempts},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(path)

    async def bind(
        self,
        condition: VerificationCondition,
        observation: dict,
    ) -> dict:
        """자연어 대상 설명을 관찰된 요소에 연결한다.

        Args:
            condition: 생성한 검증 조건 후보.
            observation: VerificationBrowser.observe의 결과.

        Returns:
            연결 결과, 관찰 식별자, 조건 식별자와 모델 시도 기록.

        Raises:
            ValueError: 관찰 형식이 잘못됐거나 모든 모델 응답이 부적절한 경우.

        Notes:
            선행 행동의 완료 여부는 이 함수에서 판정하지 않는다.
            실행 흐름에서 검사 시점에 도달한 뒤 호출해야 한다.
            화면의 최신 상태는 이후 VerificationBrowser.read가 다시 검사한다.
        """
        started = time.perf_counter()
        observation_id = observation.get("observation_id")
        if not isinstance(observation_id, str) or not observation_id.strip():
            raise ValueError("검증용 관찰 식별자가 필요합니다.")

        elements = observation.get("elements")
        if not isinstance(elements, list):
            raise ValueError("관찰된 요소 목록이 필요합니다.")

        ids = [element.get("element_id") for element in elements]
        if any(not isinstance(item, str) or not item.strip() for item in ids):
            raise ValueError("모든 관찰 요소에 식별자가 필요합니다.")
        if len(ids) != len(set(ids)):
            raise ValueError("관찰 요소 식별자는 서로 달라야 합니다.")

        attempts = []

        def finish(binding: TargetBinding) -> dict:
            usages = [attempt["usage"] for attempt in attempts]
            usage = {
                "backend": usages[-1].get("backend") if usages else None,
                "model": usages[-1].get("model") if usages else None,
                "model_calls": len(usages),
            }
            for key in ("input_tokens", "output_tokens"):
                values = [item.get(key) for item in usages]
                usage[key] = (
                    sum(values)
                    if all(type(value) is int for value in values)
                    else None
                )
            return {
                "condition_id": condition.condition_id,
                "observation_id": observation_id,
                "binding": binding,
                "attempts": attempts,
                "usage": usage,
                "elapsed_seconds": time.perf_counter() - started,
            }

        if condition.kind not in {"text", "value"}:
            return finish(TargetBinding(
                status="unsupported",
                reason="현재 화면 읽기는 text와 value만 지원합니다.",
            ))

        if not elements:
            return finish(TargetBinding(
                status="not_observed",
                reason="현재 관찰에는 연결할 요소가 없습니다. 부재 판정은 아닙니다.",
            ))

        eligible = [
            element for element in elements
            if self._compatible(condition.kind, element)
        ]
        if not eligible:
            return finish(TargetBinding(
                status="not_observed",
                reason="현재 관찰에 읽기 종류가 맞는 대상이 없습니다.",
            ))
        schema = self._generation_schema([
            element["element_id"] for element in eligible
        ])
        # 기대값과 현재 값으로 대상을 고르면 정상인 요소를 골라내는 오류가 생긴다.
        # 연결에 필요한 설명과 요소의 역할만 전달한다.
        payload = {
            "target": {
                "description": condition.target_description,
                "kind": condition.kind,
            },
            "observation": {
                "observation_id": observation_id,
                "truncated": observation.get("truncated", False),
                "elements": [
                    {
                        "element_id": element["element_id"],
                        "tag": element.get("tag", ""),
                        "name": element.get("name", ""),
                        "supports_value": element.get("supports_value") is True,
                    }
                    for element in eligible
                ],
            },
            "output_schema": schema,
        }
        messages = [
            {"role": "system", "content": TARGET_BINDING_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

        for number in range(1, self.max_attempts + 1):
            response = await self.client.generate(messages=messages, schema=schema)
            content = response["content"]
            record = {
                "attempt": number,
                "content": content,
                "usage": response.get("usage") or {},
                "validation_error": None,
            }
            attempts.append(record)

            try:
                binding = TargetBinding.model_validate_json(content)
                index = {element["element_id"]: element for element in elements}
                for candidate_id in binding.candidate_ids:
                    if candidate_id not in index:
                        raise ValueError("현재 관찰에 없는 대상은 선택할 수 없습니다.")
                    if not self._compatible(condition.kind, index[candidate_id]):
                        raise ValueError("조건 종류와 선택한 요소가 맞지 않습니다.")

                if binding.status == "bound":
                    selected = index[binding.element_id]
                    name = selected.get("name", "")
                    # 같은 이름의 호환 요소가 있으면 모델이 하나를 골라도 보류한다.
                    peers = [
                        element["element_id"] for element in elements
                        if name and element.get("name") == name
                        and self._compatible(condition.kind, element)
                    ]
                    if len(peers) > 1:
                        binding = TargetBinding(
                            status="ambiguous",
                            candidate_ids=peers,
                            reason="같은 이름의 대상이 여러 개여서 하나로 확정하지 않습니다.",
                        )

            except ValueError as error:
                record["validation_error"] = str(error)
                print(
                    f"대상 연결 응답 검사 실패 ({number}/{self.max_attempts}): {error}"
                )
                if number == self.max_attempts:
                    diagnostic_path = self._save_failure(payload, attempts)
                    suffix = f" 기록: {diagnostic_path}" if diagnostic_path else ""
                    raise ValueError(
                        f"대상 연결 응답이 검사를 통과하지 못했습니다: {error}{suffix}"
                    ) from error
                messages.extend([
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": json.dumps(
                        {"validation_error": str(error), "output_schema": schema},
                        ensure_ascii=False
                    )},
                ])
                continue

            record["resolved_binding"] = binding.model_dump(mode="json")
            return finish(binding)

        raise RuntimeError("대상 연결이 정상적으로 종료되지 않았습니다.")
