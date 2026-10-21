"""제보와 화면 관찰을 바탕으로 실행 가능한 행동을 선택한다."""

import json
from time import perf_counter

from app.agent.action_schema import NextDecision
from app.agent.checkpoint_reviewer import CheckpointReviewer
from app.agent.model_client import JsonModelClient
from app.agent.prompts import ACTION_PROMPT
from app.agent.report_schema import BugSpec


def validate_action_target(
    decision: NextDecision,
    observation: dict,
) -> None:
    """관찰 도구가 허용한 행동인지 검사한다.

    Args:
        decision: 모델이 선택한 다음 결정.
        observation: 선택의 근거가 된 화면 관찰.

    Returns:
        없음.

    Raises:
        ValueError: 대상이 없거나 사용할 수 없는 행동인 경우.

    Notes:
        HTML 태그만으로 실행 가능 여부를 추측하지 않는다.
        실제 실행 직전의 대상 상태는 브라우저 도구가 다시 검사한다.
    """
    if decision.decision != "act":
        return

    action = decision.action

    if action is None:
        raise ValueError("행동 정보가 없습니다.")

    matches = [
        element
        for element in observation["elements"]
        if element["element_id"] == action.element_id
    ]

    if len(matches) != 1:
        raise ValueError(
            "행동 대상을 관찰에서 하나로 확인할 수 없습니다."
        )

    target = matches[0]

    if not target["enabled"]:
        raise ValueError(
            "비활성화된 요소에는 행동할 수 없습니다."
        )

    allowed_actions = target.get("actions", [])

    if action.kind not in allowed_actions:
        raise ValueError(
            "관찰 도구가 해당 요소에 허용하지 않은 행동입니다."
        )


def history_view(history):
    """과거 식별자를 제거한 행동 번호와 대상 역할, 전후 상태를 전달한다."""
    def elements(items):
        return [{key: item.get(key) for key in ['tag', 'name', 'value', 'enabled', 'actions']} for item in items]
    result = []
    for number, item in enumerate(history, 1):
        action = item['action']
        target = next((entry for entry in item.get('before_elements', [])
                       if entry.get('element_id') == action.get('element_id')), {})
        result.append({
            'action_number': number,
            'executed_action': {'kind': action['kind'], 'value': action.get('value'),
                                'target_name': target.get('name', ''), 'target_tag': target.get('tag', '')},
            'execution_success': item.get('success') is True, 'error_type': item.get('error_type'),
            'before_elements': elements(item.get('before_elements', [])),
            'after_elements': elements(item.get('after_elements', [])),
        })
    return result


def action_schema(spec, observation):
    """현재 허용 대상과 입력값, 행동별 null 제약을 생성 형식에 반영한다."""
    branches = []
    def branch(decision, action, question):
        return {'type': 'object', 'additionalProperties': False,
                'required': ['decision', 'description', 'action', 'question'],
                'properties': {'decision': {'type': 'string', 'enum': [decision]},
                               'description': {'type': 'string', 'minLength': 1},
                               'action': action, 'question': question}}
    values = list(dict.fromkeys(item.value for item in spec.inputs))
    for kind in ['fill', 'click', 'reload']:
        ids = [item['element_id'] for item in observation['elements']
               if item.get('enabled') is True and kind in item.get('actions', [])]
        if not ids or (kind == 'fill' and not values):
            continue
        action = {'type': 'object', 'additionalProperties': False,
                  'required': ['kind', 'element_id', 'value'],
                  'properties': {'kind': {'type': 'string', 'enum': [kind]},
                                 'element_id': {'type': 'string', 'enum': ids},
                                 'value': {'type': 'string', 'enum': values} if kind == 'fill' else {'type': 'null'}}}
        branches.append(branch('act', action, {'type': 'null'}))
    branches.append(branch('finish', {'type': 'null'}, {'type': 'null'}))
    branches.append(branch('need_information', {'type': 'null'}, {'type': 'string', 'minLength': 1}))
    return {'anyOf': branches}


class ActionPlanner:
    """행동 완료의 근거 확인과 현재 화면의 다음 행동 선택을 연결한다."""

    def __init__(self, client: JsonModelClient):
        """모델 연결과 기존 행동 근거 검토기를 재사용한다."""
        self.client = client
        self.completion_reviewer = CheckpointReviewer(client, max_attempts=1)

    @staticmethod
    def _usage(usages):
        """완료 검토를 포함한 실제 모델 호출의 사용량을 합산한다."""
        last = usages[-1] if usages else {}
        result = {'backend': last.get('backend'), 'model': last.get('model'), 'model_calls': len(usages)}
        for key in ['input_tokens', 'output_tokens']:
            values = [item.get(key) for item in usages]
            result[key] = sum(values) if all(type(value) is int for value in values) else None
        return result

    async def next_action(self, spec: BugSpec, observation: dict, history: list[dict]) -> dict:
        """완료를 확인하면 조작을 끝내고 아니면 허용된 다음 행동을 선택한다.

        Args:
            spec: 구조화된 제보와 사용자가 제공한 입력값.
            observation: 현재 화면의 식별자와 허용 행동.
            history: 실제 행동과 전후 관찰 기록.

        Returns:
            결정, 모델 사용량, 완료 검토 근거와 소요 시간.

        Notes:
            조작 종료는 재현 성공이나 정상 상태를 뜻하지 않는다.
            최종 조건 비교와 재현 판정은 기존 후속 단계가 수행한다.
        """
        started = perf_counter()
        usages = []
        completion = None
        if spec.reported_steps and len(history) >= len(spec.reported_steps):
            checked = await self.completion_reviewer.review_completion(spec, history)
            completion = {'review': checked['review'].model_dump(mode='json'), 'attempts': checked['attempts']}
            usages.extend(item.get('usage') or {} for item in checked['attempts'])
            if checked['review'].status == 'reached':
                return {'decision': NextDecision(decision='finish', description='실행 기록으로 제보의 조작 완료를 확인했습니다.',
                                                  action=None, question=None),
                        'usage': self._usage(usages), 'completion_review': completion,
                        'elapsed_seconds': round(perf_counter() - started, 2)}

        schema = action_schema(spec, observation)
        payload = {
            'bug_spec': spec.model_dump(mode='json'),
            'observation': {'elements': observation['elements'], 'truncated': observation.get('truncated', False)},
            'history': history_view(history),
            'history_note': '행동 번호는 실제 실행 기록이다. 대상 식별자는 현재 관찰에서만 선택한다. '
                            '정상 결과를 만들려고 완료한 제보의 조작을 반복하지 않는다.',
            'output_schema': schema,
        }
        response = await self.client.generate(messages=[
            {'role': 'system', 'content': ACTION_PROMPT},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
        ], schema=schema)
        usages.append(response.get('usage') or {})
        decision = NextDecision.model_validate_json(response['content'])
        validate_action_target(decision, observation)
        if decision.decision == 'act' and decision.action.kind == 'fill':
            if decision.action.value not in {item.value for item in spec.inputs}:
                raise ValueError('제보와 답변에 없는 입력값은 사용할 수 없습니다.')
        return {'decision': decision, 'usage': self._usage(usages), 'completion_review': completion,
                'elapsed_seconds': round(perf_counter() - started, 2)}
