"""추가 답변의 근거 보존과 후속 실행 연결을 API에서 검사한다."""

import argparse
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.run_service import RunService
from app.main import create_app


class AnswerApiTests(unittest.TestCase):
    """검사 대역으로 답변 검증, 이력 보존과 중복 제출을 확인한다."""

    def setUp(self):
        """원래 요청과 전달된 답변을 확인할 임시 API를 준비한다."""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.received = []

        async def worker(request, run_id, folder, *, answers=None):
            history = [item.model_dump() for item in answers or []]
            self.received.append({'run_id': run_id, 'report': request.report,
                                  'target': request.target.model_dump(mode='json'), 'answers': history})
            if request.report == '대기 작업' and history:
                await asyncio.Event().wait()
            required = 2 if request.report == '두 회차 확인' else 1
            needs = len(history) < required or request.report == '계속 질문'
            return {'workflow_status': 'needs_information' if needs else 'conditions_checked',
                    'reproduction_status': 'needs_information' if needs else 'reproduced',
                    'questions': [f'정상 기대 동작을 설명해 주세요. 확인 회차 {len(history) + 1}'] if needs else [],
                    'interpretation': {'answers': history}, 'error': None}

        self.service = RunService(self.root, worker)
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()

    def tearDown(self):
        """실행 중 작업과 검사 자료를 정리한다."""
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def wait(self, run_id, statuses=None):
        """접수한 작업이 원하는 상태에 도달했는지 확인한다."""
        statuses = statuses or {'completed', 'needs_information', 'failed'}
        for _ in range(200):
            response = self.client.get(f'/api/runs/{run_id}')
            self.assertEqual(response.status_code, 200)
            state = response.json()
            if state['status'] in statuses:
                return state
            time.sleep(0.005)
        self.fail(f'예상 상태에 도달하지 못했습니다: {state}')

    def submit(self, report='내용이 사라집니다.'):
        """답변이 필요한 제보를 접수한다."""
        response = self.client.post('/api/runs', json={
            'report': report, 'target': {'start_url': 'http://example.test/',
                                       'public_spec': '사용자가 제공한 요구사항'}})
        self.assertEqual(response.status_code, 202)
        return self.wait(response.json()['run_id'])

    def send(self, state, text='저장한 내용이 유지돼야 합니다.'):
        """서버가 발급한 실제 질문 식별자로 답변한다."""
        return self.client.post(f"/api/runs/{state['run_id']}/answers", json={
            'answers': {item['question_id']: text for item in state['questions']}})

    def test_answer_and_original_preserved(self):
        """새 실행에 답변을 전달하고 이전 결과와 원래 요청을 보존한다."""
        parent = self.submit()
        before = self.client.get(parent['result_url']).json()
        response = self.send(parent)
        self.assertEqual(response.status_code, 202)
        child = self.wait(response.json()['run_id'])
        self.assertEqual(child['status'], 'completed')
        self.assertEqual(child['parent_run_id'], parent['run_id'])
        self.assertEqual(child['root_run_id'], parent['run_id'])
        self.assertEqual(child['clarification_round'], 1)
        self.assertEqual(self.client.get(parent['result_url']).json(), before)
        received = self.received[-1]
        self.assertEqual(received['report'], '내용이 사라집니다.')
        self.assertEqual(received['target']['public_spec'], '사용자가 제공한 요구사항')
        self.assertEqual(received['answers'][0]['question'], parent['questions'][0]['question'])
        self.assertEqual(received['answers'][0]['answer'], '저장한 내용이 유지돼야 합니다.')

    def test_missing_answer(self):
        """답변이 비어 있거나 질문 식별자가 빠진 요청을 거부한다."""
        parent = self.submit()
        self.assertEqual(self.client.post(f"/api/runs/{parent['run_id']}/answers",
                         json={'answers': {}}).status_code, 422)
        self.assertEqual(self.send(parent, '   ').status_code, 422)

    def test_unknown_question(self):
        """사용자가 만든 질문이나 다른 작업의 질문을 근거로 받지 않는다."""
        parent, other = self.submit(), self.submit()
        payload = {'answers': {other['questions'][0]['question_id']: '정상 동작'}}
        self.assertEqual(self.client.post(f"/api/runs/{parent['run_id']}/answers", json=payload).status_code, 422)

    def test_extra_question(self):
        """유효한 질문과 함께 보낸 출처 없는 질문도 거부한다."""
        parent = self.submit()
        payload = {parent['questions'][0]['question_id']: '정상 동작', 'invented': '추가 문장'}
        self.assertEqual(self.client.post(f"/api/runs/{parent['run_id']}/answers",
                         json={'answers': payload}).status_code, 422)

    def test_long_answer(self):
        """정해진 답변 길이 제한을 검사한다."""
        self.assertEqual(self.send(self.submit(), 'a' * 10001).status_code, 422)

    def test_duplicate_submission(self):
        """동일한 답변을 다시 보내도 후속 작업을 중복 생성하지 않는다."""
        parent = self.submit()
        one = self.send(parent)
        two = self.send(parent)
        self.assertEqual(one.json()['run_id'], two.json()['run_id'])
        self.wait(one.json()['run_id'])
        self.assertEqual(len(self.received), 2)

    def test_changed_duplicate(self):
        """이미 사용한 질문의 답변을 바꿔 별도 실행을 만들지 않는다."""
        parent = self.submit()
        self.send(parent)
        self.assertEqual(self.send(parent, '다른 답변').status_code, 409)

    def test_completed_rejected(self):
        """추가 질문 없이 끝난 작업에는 답변을 제출할 수 없다."""
        child = self.wait(self.send(self.submit()).json()['run_id'])
        self.assertEqual(self.client.post(f"/api/runs/{child['run_id']}/answers",
                         json={'answers': {'unknown': '답변'}}).status_code, 409)

    def test_unknown_run(self):
        """없는 작업에 대한 답변은 찾을 수 없음으로 응답한다."""
        self.assertEqual(self.client.post('/api/runs/unknown/answers',
                         json={'answers': {}}).status_code, 404)

    def test_accumulated_answers(self):
        """여러 회차의 질문과 답변을 순서대로 누적한다."""
        parent = self.submit('두 회차 확인')
        first = self.wait(self.send(parent, '첫 번째 답변').json()['run_id'])
        self.assertEqual(first['status'], 'needs_information')
        second = self.wait(self.send(first, '두 번째 답변').json()['run_id'])
        self.assertEqual(second['status'], 'completed')
        self.assertEqual(second['root_run_id'], parent['run_id'])
        self.assertEqual([item['answer'] for item in self.received[-1]['answers']],
                         ['첫 번째 답변', '두 번째 답변'])

    def test_restore_and_answer(self):
        """서버 재시작 후에도 저장한 질문에 답변할 수 있다."""
        parent = self.submit()
        restored = RunService(self.root, self.service.worker)
        with TestClient(create_app(restored)) as client:
            response = client.post(f"/api/runs/{parent['run_id']}/answers", json={
                'answers': {parent['questions'][0]['question_id']: '사용자 답변'}})
            self.assertEqual(response.status_code, 202)

    def test_restore_duplicate(self):
        """서버 재시작 후 중복 제출도 기존 후속 작업을 반환한다."""
        parent = self.submit()
        child = self.wait(self.send(parent).json()['run_id'])
        restored = RunService(self.root, self.service.worker)
        with TestClient(create_app(restored)) as client:
            response = client.post(f"/api/runs/{parent['run_id']}/answers", json={
                'answers': {parent['questions'][0]['question_id']: '저장한 내용이 유지돼야 합니다.'}})
            self.assertEqual(response.json()['run_id'], child['run_id'])

    def test_legacy_questions(self):
        """이전 단계의 질문 식별자가 없는 저장 기록도 보완해 조회한다."""
        parent = self.submit()
        path = self.service.folder(parent['run_id']) / 'api_state.json'
        record = json.loads(path.read_text())
        record.pop('questions')
        path.write_text(json.dumps(record))
        restored = RunService(self.root)
        self.assertEqual(restored.get(parent['run_id'])['questions'], parent['questions'])

    def test_round_limit(self):
        """반복 답변 연결의 제한을 넘으면 새 실행을 만들지 않는다."""
        state = self.submit('계속 질문')
        for _ in range(5):
            response = self.send(state)
            self.assertEqual(response.status_code, 202)
            state = self.wait(response.json()['run_id'])
        self.assertEqual(self.send(state).status_code, 409)

    def test_cancel_child(self):
        """후속 실행을 취소해도 이전 질문 결과는 유지된다."""
        parent = self.submit('대기 작업')
        child = self.send(parent).json()
        self.wait(child['run_id'], {'running'})
        self.client.post(f"/api/runs/{child['run_id']}/cancel")
        self.wait(child['run_id'], {'cancelled'})
        self.assertEqual(self.client.get(f"/api/runs/{parent['run_id']}").json()['status'], 'needs_information')


def wait_live(client, job):
    """실제 모델을 사용하는 작업의 종료를 기다린다."""
    deadline = time.monotonic() + 360
    previous = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{job['run_id']}")
        response.raise_for_status()
        state = response.json()
        if previous != state['status']:
            print('작업 상태:', state['status'])
            previous = state['status']
        if state['status'] not in {'queued', 'running', 'cancelling'}:
            return state
        time.sleep(0.5)
    client.post(f"/api/runs/{job['run_id']}/cancel")
    raise TimeoutError('제한 시간에 도달해 취소를 요청했습니다.')


def live_demo(version):
    """불명확한 제보에 실제 질문을 받고 사용자 답변으로 다시 실행한다."""
    from scripts.check_reproduction_workflow import start_demo

    server, url = start_demo(version)
    try:
        with TestClient(create_app()) as client:
            response = client.post('/api/runs', json={
                'report': '저장 기능이 이상합니다.', 'target': {'start_url': url}})
            response.raise_for_status()
            parent = wait_live(client, response.json())
            print('첫 작업:', parent['run_id'])
            print('확인 질문:', json.dumps(parent.get('questions'), ensure_ascii=False, indent=2))
            assert parent['status'] == 'needs_information', '불명확한 제보의 추가 질문을 확인하세요.'
            original = client.get(parent['result_url']).json()
            detail = ('문서 제목 입력란에 주간 계획을 입력하고 저장 버튼을 누른 뒤 새로고침했습니다. '
                      '실제 현상은 제목이 비어 있다는 것입니다. '
                      '정상 동작은 새로고침 후에도 문서 제목이 주간 계획으로 유지되는 것입니다.')
            response = client.post(f"/api/runs/{parent['run_id']}/answers", json={
                'answers': {item['question_id']: detail for item in parent['questions']}})
            response.raise_for_status()
            child = wait_live(client, response.json())
            print('후속 작업:', child['run_id'])
            result_response = client.get(child['result_url'])
            result_response.raise_for_status()
            result = result_response.json()
            print('최종 재현 상태:', result['reproduction_status'])
            print('해석 결과:', json.dumps(result.get('interpretation'), ensure_ascii=False, indent=2))
            print('테스트 생성:', json.dumps(result.get('test_generation'), ensure_ascii=False, indent=2))
            if result.get('questions'):
                print('추가 질문:', result['questions'])
            assert client.get(parent['result_url']).json() == original, '이전 결과가 변경됐습니다.'
            assert child['parent_run_id'] == parent['run_id'], '이전 작업 연결이 필요합니다.'
            assert result['interpretation']['spec']['expectation_source'] == 'user_answer', '답변 근거를 확인하세요.'
            expected = 'reproduced' if version == 'buggy' else 'not_reproduced'
            assert result['reproduction_status'] == expected, '예상 재현 상태와 다릅니다.'
            assert result['test_generation']['test_status'] == 'generated', '테스트 생성 결과를 확인하세요.'
            print('PASS: 추가 질문, 답변 근거 전달, 후속 실행과 이전 결과 보존')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-demo', choices=['buggy', 'fixed'])
    args = parser.parse_args()
    if args.live_demo:
        live_demo(args.live_demo)
    else:
        unittest.main(argv=['check_answer_api'], verbosity=2)
