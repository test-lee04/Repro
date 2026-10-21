"""행동 완료, 현재 대상 선택과 읽기 가능 정보 전달을 모델 없이 검사한다."""

import json
import tempfile
import unittest
from pathlib import Path

from playwright.async_api import async_playwright

from app.agent.action_planner import ActionPlanner
from app.agent.action_schema import NextDecision
from app.agent.checkpoint_reviewer import CheckpointValidationError
from app.agent.report_schema import BugSpec
from app.agent.reproduction_loop import ReproductionLoop
from app.agent.target_binder import TargetBinder
from app.browser.verification import VerificationBrowser
from app.testing.check_schema import VerificationCondition
from app.testing.condition_comparator import compare_condition


def spec(steps=None):
    """입력·확정·새로고침을 요구하는 일반 제보를 준비한다."""
    return BugSpec(summary='표시 이름 유지', reported_steps=steps or ['표시 이름을 입력합니다.', '보관 버튼을 누릅니다.', '새로고침합니다.'],
                   inputs=[{'name': '표시 이름', 'value': '업무'}], actual_behavior='값이 비어 있습니다.',
                   expected_behavior='표시 이름은 업무로 유지돼야 합니다.', expectation_source='report',
                   expectation_evidence='표시 이름은 업무로 유지돼야 합니다.', missing_information=[])


def observation(value=''):
    """현재 식별자와 허용 행동을 가진 화면을 준비한다."""
    return {'observation_id': 'current-observation', 'elements': [
        {'element_id': 'current-input', 'tag': 'input', 'name': '표시 이름', 'value': value, 'enabled': True, 'actions': ['fill']},
        {'element_id': 'current-save', 'tag': 'button', 'name': '보관', 'value': '보관', 'enabled': True, 'actions': ['click']},
        {'element_id': 'current-page', 'tag': 'page', 'name': '현재 페이지', 'value': 'http://example.test', 'enabled': True, 'actions': ['reload']},
    ]}


def history():
    """실제 행동 번호에 대응하는 완료 기록을 준비한다."""
    result = []
    for kind, identifier, tag, name, value in [
        ('fill', 'old-input', 'input', '표시 이름', '업무'),
        ('click', 'old-save', 'button', '보관', None),
        ('reload', 'old-page', 'page', '현재 페이지', None),
    ]:
        result.append({'action': {'kind': kind, 'element_id': identifier, 'value': value, 'observation_id': 'old-observation'},
                       'success': True, 'error_type': None,
                       'before_elements': [{'element_id': identifier, 'tag': tag, 'name': name, 'value': value}],
                       'after_elements': []})
    return result


def completion(reached=True):
    """보고한 단계와 실행 번호를 대조한 검토 결과를 만든다."""
    return {'status': 'reached' if reached else 'inconclusive',
            'coverage': [{'step_number': index, 'action_numbers': [index], 'reason': '실제 조작 기록 확인'} for index in range(1, 4)] if reached else [],
            'checkpoint_action_numbers': [3] if reached else [],
            'reason': '제보의 조작을 모두 수행했습니다.' if reached else '아직 필요한 조작이 남았습니다.'}


def decision(kind='click', element_id='current-save', value=None):
    """검사에 사용할 다음 행동 응답을 만든다."""
    return {'decision': 'act', 'description': '다음 조작을 수행합니다.',
            'action': {'kind': kind, 'element_id': element_id, 'value': value}, 'question': None}


class Client:
    """준비한 모델 응답을 반환하고 실제 요청 자료를 보관한다."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.payloads = []

    async def generate(self, messages, schema):
        """형식과 실제 호출 횟수를 확인할 대역 응답을 반환한다."""
        self.payloads.append(json.loads(messages[1]['content']))
        return {'content': json.dumps(self.outputs.pop(0), ensure_ascii=False),
                'usage': {'backend': 'fixture', 'model': 'fixture', 'input_tokens': 10, 'output_tokens': 5}}


class ExecutionContractTests(unittest.IsolatedAsyncioTestCase):
    """재현 성공과 별개로 완료한 조작을 보존하는지 확인한다."""

    async def test_complete_actions_stop_with_empty_or_kept_value(self):
        """현재 값이 비었거나 유지돼도 조작 완료를 확인하면 재입력하지 않는다."""
        for value in ['', '업무']:
            client = Client([completion()])
            result = await ActionPlanner(client).next_action(spec(), observation(value), history())
            self.assertEqual(result['decision'].decision, 'finish')
            self.assertEqual(result['usage']['model_calls'], 1)
            self.assertEqual(len(client.payloads), 1)

    async def test_unfinished_actions_continue(self):
        """아직 조작이 남았으면 종료를 강제하지 않는다."""
        client = Client([completion(False), decision()])
        report = spec(['입력하고 보관한 뒤 새로고침합니다.'])
        result = await ActionPlanner(client).next_action(report, observation(), history()[:1])
        self.assertEqual(result['decision'].decision, 'act')
        self.assertEqual(result['usage']['model_calls'], 2)

    async def test_invalid_completion_reference_rejected(self):
        """존재하지 않는 행동 근거로 종료할 수 없다."""
        invalid = completion()
        invalid['coverage'][2]['action_numbers'] = [99]
        with self.assertRaises(CheckpointValidationError):
            await ActionPlanner(Client([invalid])).next_action(spec(), observation(), history())

    async def test_old_identifiers_not_forwarded(self):
        """과거 행동은 보존하되 과거 대상 식별자를 모델에게 다시 주지 않는다."""
        client = Client([decision()])
        await ActionPlanner(client).next_action(spec(), observation(), history()[:1])
        payload = client.payloads[0]
        text = json.dumps(payload['history'])
        self.assertNotIn('old-input', text)
        self.assertNotIn('old-observation', text)
        self.assertEqual(payload['history'][0]['executed_action']['value'], '업무')
        self.assertEqual(payload['observation']['elements'][1]['element_id'], 'current-save')

    async def test_action_schema_restricts_current_ids_and_null(self):
        """현재 대상과 클릭의 null 입력값을 생성 형식에서도 제한한다."""
        client = Client([decision()])
        await ActionPlanner(client).next_action(spec(), observation(), [])
        branches = client.payloads[0]['output_schema']['anyOf']
        actions = [item['properties']['action'] for item in branches if item['properties']['decision']['enum'] == ['act']]
        click = next(item for item in actions if item['properties']['kind']['enum'] == ['click'])
        self.assertEqual(click['properties']['value'], {'type': 'null'})
        self.assertEqual(click['properties']['element_id']['enum'], ['current-save'])

    async def test_unknown_target_still_rejected(self):
        """모델이 형식을 무시해도 현재 관찰 밖의 대상은 실행하지 않는다."""
        with self.assertRaises(ValueError):
            await ActionPlanner(Client([decision(element_id='old-save')])).next_action(spec(), observation(), [])

    async def test_unprovided_input_rejected(self):
        """원문 입력값에 없는 새 값을 모델이 만들어도 거부한다."""
        with self.assertRaises(ValueError):
            await ActionPlanner(Client([decision('fill', 'current-input', '만든 값')])).next_action(spec(), observation(), [])

    async def test_loop_error_preserves_history(self):
        """계획 오류로 종료해도 성공한 이전 행동을 결과와 파일에 보존한다."""
        class Planner:
            calls = 0
            async def next_action(self, **kwargs):
                self.calls += 1
                if self.calls == 2:
                    raise ValueError('검사용 다음 행동 오류')
                return {'decision': NextDecision.model_validate(decision('fill', 'current-input', '업무')),
                        'usage': {}, 'elapsed_seconds': 0}
        class Tools:
            latest = {'session': observation('업무')}
            async def observe(self, session_id):
                return observation()
            async def execute(self, **kwargs):
                return {'success': True, 'error_type': None}
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            result = await ReproductionLoop(Planner(), Tools()).run(spec(), 'session', folder)
            self.assertEqual(result['loop_status'], 'error')
            self.assertEqual(len(result['history']), 1)
            self.assertEqual(len(json.loads((folder / 'loop_result.json').read_text())['history']), 1)


class OutputReadTests(unittest.IsolatedAsyncioTestCase):
    """실제 output 요소를 문구 읽기 후보로 연결해 비교한다."""

    async def test_output_is_readable_text(self):
        """입력값 지원이 없는 표시 요소에서도 실제 문구를 읽는다."""
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content('<output aria-label="처리 결과">추가 정보 필요.</output>')
                class Sessions:
                    def get_page(self, session_id):
                        return page
                with tempfile.TemporaryDirectory() as temporary:
                    tools = VerificationBrowser(Sessions(), Path(temporary))
                    observed = await tools.observe('session')
                    target = next(item for item in observed['elements'] if item['tag'] == 'output')
                    condition = VerificationCondition(condition_id='output-message', kind='text', target_description='처리 결과의 표시 문구',
                                                      operator='contains', expected_value='추가 정보 필요', source='report',
                                                      evidence='처리 결과에 추가 정보 필요라는 문구가 표시돼야 합니다.', checkpoint='처리 후')
                    client = Client([{'status': 'bound', 'element_id': target['element_id'],
                                      'candidate_ids': [target['element_id']], 'reason': '이름이 같은 처리 결과 요소입니다.'}])
                    binding = await TargetBinder(client).bind(condition, observed)
                    sent = client.payloads[0]['observation']['elements']
                    self.assertEqual(sent[0]['readable_kinds'], ['text'])
                    self.assertNotIn('supports_value', sent[0])
                    read = await tools.read('session', binding['observation_id'], binding['binding'].element_id, 'text')
                    self.assertEqual(read['actual_value'], '추가 정보 필요.')
                    result = compare_condition(condition, binding, read, checkpoint_reached=True)
                    self.assertEqual(result.status, 'satisfied')
            finally:
                await browser.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
