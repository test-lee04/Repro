"""재실행 API의 작업 관리와 기존 수정 검증 규칙 연결을 검사한다."""

import argparse
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.replay_service import ReplayService
from app.api.run_service import RunService
from app.main import create_app
from app.testing.compiler import RecordedTestCompiler
from app.testing.replay import RecordedReplayRunner
from scripts.check_test_compiler import fixture_run


def make_result(folder):
    """검사용 실제 실행 기록으로 기존 생성기를 호출한다."""
    result = fixture_run()
    compiler = RecordedTestCompiler()
    result['test_generation'] = {
        **compiler.export(compiler.compile_run(result), folder / 'regression'),
        'generation_status': 'completed', 'replay_status': 'not_run', 'reason': '검사용 테스트 생성 완료',
    }
    return result


async def replay_executor(case, url, folder, result, timeout):
    """API 검사에만 쓰는 실행 대역으로 정상·실패·대기 결과를 만든다."""
    if url.endswith('/wait'):
        await asyncio.Event().wait()
    if url.endswith('/error'):
        raise RuntimeError('검사용 실행 오류')
    passed = url.endswith('/fixed')
    result['actions'] = [{'action': action, 'success': True} for action in case['actions']]
    result['assertions'] = []
    for item in case['assertions']:
        condition = item['condition']
        result['assertions'].append({
            **{key: condition[key] for key in ['condition_id', 'kind', 'operator', 'expected_value']},
            'actual_value': condition['expected_value'] if passed else '', 'passed': passed,
        })
    result['status'] = 'passed' if passed else 'assertion_failed'


class ReplayApiTests(unittest.TestCase):
    """모델 없이 기존 재실행기의 검증 규칙을 API 경로에서 확인한다."""

    def setUp(self):
        """임시 저장 위치와 실행 대역을 사용하는 앱을 준비한다."""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

        async def worker(request, run_id, folder):
            return make_result(folder)

        self.runs = RunService(self.root, worker)
        self.replays = ReplayService(self.runs, RecordedReplayRunner(executor=replay_executor))
        self.client = TestClient(create_app(self.runs, self.replays))
        self.client.__enter__()
        response = self.client.post('/api/runs', json={
            'report': '검사용 제보', 'target': {'start_url': 'http://example.test/'}})
        self.run_id = response.json()['run_id']
        self.wait(f'/api/runs/{self.run_id}')
        self.base = f'/api/runs/{self.run_id}/replays'

    def tearDown(self):
        """작업을 정리하고 저장 자료를 제거한다."""
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def wait(self, path):
        """대기와 실행 중 상태가 종료될 때까지 검사한다."""
        for _ in range(200):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            state = response.json()
            if state['status'] not in {'queued', 'running', 'cancelling'}:
                return state
            time.sleep(0.005)
        self.fail(f'예상 종료 상태에 도달하지 못했습니다: {state}')

    def submit(self, purpose='original', path='buggy', request_id=None):
        """명시한 환경 역할과 주소로 재실행을 접수한다."""
        return self.client.post(self.base, json={
            'target_url': f'http://example.test/{path}', 'purpose': purpose,
            'request_id': request_id or uuid4().hex})

    def replay(self, purpose='original', path='buggy'):
        """재실행의 종료 결과를 조회한다."""
        response = self.submit(purpose, path)
        self.assertEqual(response.status_code, 202)
        return self.wait(f"{self.base}/{response.json()['replay_id']}")

    def test_original_and_fix(self):
        """동일한 테스트의 원래 환경 실패 후 수정 환경 통과를 연결한다."""
        before = self.client.get(f'/api/runs/{self.run_id}/result').json()
        original = self.replay()
        self.assertEqual(original['result']['test_generation']['test_status'], 'awaiting_fix_validation')
        fix = self.replay('fix', 'fixed')
        self.assertEqual(fix['result']['test_generation']['test_status'], 'validated')
        after = self.client.get(f'/api/runs/{self.run_id}/result').json()
        self.assertEqual(after['assessment'], before['assessment'])
        self.assertEqual(after['reproduction_status'], before['reproduction_status'])
        self.assertEqual(after['test_generation']['test_status'], 'validated')
        self.assertEqual(original['result']['replay']['artifact_fingerprint'], fix['result']['replay']['artifact_fingerprint'])

    def test_modified_expectation_clears_validation(self):
        """검증 완료 후 기대값을 바꾸면 이전 완료 상태를 현재 결과로 쓰지 않는다."""
        self.replay()
        self.replay('fix', 'fixed')
        path = self.runs.folder(self.run_id) / 'regression' / 'case.json'
        case = json.loads(path.read_text())
        case['assertions'][0]['condition']['expected_value'] = '바꾼 기대값'
        path.write_text(json.dumps(case))
        record = self.replay()
        self.assertEqual(record['status'], 'failed')
        result = self.client.get(f'/api/runs/{self.run_id}/result').json()
        self.assertEqual(result['test_generation']['validation_status'], 'invalid_test')
        self.assertEqual(result['test_generation']['test_status'], 'unsupported')
        self.assertEqual(result['reproduction_status'], 'reproduced')

    def test_fix_alone(self):
        """수정 환경 통과만으로 검증 완료를 표시하지 않는다."""
        state = self.replay('fix', 'fixed')
        self.assertEqual(state['result']['test_generation']['validation_status'], 'baseline_missing')

    def test_error(self):
        """실행 오류를 조건 실패나 수정 검증 완료로 바꾸지 않는다."""
        state = self.replay('original', 'error')
        self.assertEqual(state['result']['replay']['status'], 'replay_error')
        self.assertEqual(state['result']['test_generation']['test_status'], 'environment_error')

    def test_duplicate_request(self):
        """같은 접수 식별자로 재실행을 중복 생성하지 않는다."""
        key = uuid4().hex
        one = self.submit(request_id=key)
        two = self.submit(request_id=key)
        self.assertEqual(one.json()['replay_id'], two.json()['replay_id'])
        self.assertEqual(len(self.client.get(self.base).json()['replays']), 1)

    def test_changed_duplicate(self):
        """같은 접수 식별자로 주소를 바꾼 요청은 거부한다."""
        key = uuid4().hex
        self.submit(request_id=key)
        self.assertEqual(self.submit(path='fixed', request_id=key).status_code, 409)

    def test_active_replay_rejected(self):
        """한 작업에 여러 재실행을 동시에 접수하지 않는다."""
        response = self.submit(path='wait')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.submit().status_code, 409)

    def test_cancel(self):
        """재실행 취소를 기록하고 원래 작업은 종료 상태로 유지한다."""
        response = self.submit(path='wait')
        path = f"{self.base}/{response.json()['replay_id']}"
        for _ in range(100):
            if self.client.get(path).json()['status'] == 'running':
                break
            time.sleep(0.005)
        self.assertEqual(self.client.post(path + '/cancel').status_code, 202)
        self.assertEqual(self.wait(path)['status'], 'cancelled')
        self.assertEqual(self.client.get(f'/api/runs/{self.run_id}').json()['status'], 'completed')

    def test_invalid_request(self):
        """잘못된 환경 역할과 파일 주소를 거부한다."""
        for purpose, url in [('other', 'http://example.test'), ('original', 'file:///tmp/test')]:
            response = self.client.post(self.base, json={'purpose': purpose, 'target_url': url, 'request_id': uuid4().hex})
            self.assertEqual(response.status_code, 422)

    def test_unknown_owner(self):
        """없는 작업의 결과 폴더를 지정할 수 없다."""
        self.assertEqual(self.client.get('/api/runs/unknown/replays').status_code, 404)

    def test_owner_isolation(self):
        """다른 원래 작업으로 재실행 식별자를 조회할 수 없다."""
        state = self.replay()
        another = self.client.post('/api/runs', json={'report': '다른 제보', 'target': {'start_url': 'http://example.test/'}}).json()['run_id']
        self.wait(f'/api/runs/{another}')
        self.assertEqual(self.client.get(f"/api/runs/{another}/replays/{state['replay_id']}").status_code, 404)

    def test_missing_test(self):
        """생성 파일이 없는 작업에는 재실행을 접수하지 않는다."""
        (self.runs.folder(self.run_id) / 'regression' / 'case.json').unlink()
        self.assertEqual(self.submit().status_code, 422)

    def test_restore(self):
        """서버 재시작 후 재실행 이력과 수정 검증 상태를 조회한다."""
        record = self.replay()
        runs = RunService(self.root)
        restored = ReplayService(runs)
        self.assertEqual(restored.get(self.run_id, record['replay_id'])['status'], 'completed')
        self.assertEqual(runs.result(self.run_id)['test_generation']['test_status'], 'awaiting_fix_validation')

    def test_artifacts(self):
        """재실행 결과 파일도 기존 다운로드 경로에서 조회한다."""
        self.replay()
        files = self.client.get(f'/api/runs/{self.run_id}/artifacts').json()['artifacts']
        self.assertTrue(any(item['name'].endswith('/replay_result.json') for item in files))


def serve_demo(port):
    """원래·수정 검사 사이트를 동시에 유지하며 실제 사용자 화면을 연다."""
    import uvicorn
    from scripts.check_reproduction_workflow import start_demo

    buggy, original_url = start_demo('buggy')
    fixed, fixed_url = start_demo('fixed')
    try:
        print(f'사용자 화면: http://127.0.0.1:{port}', flush=True)
        print(f'원래 환경 URL: {original_url}', flush=True)
        print(f'수정 환경 URL: {fixed_url}', flush=True)
        print('기존 버그 재현 작업을 불러온 뒤 두 URL로 같은 테스트를 순서대로 재실행하세요.', flush=True)
        print('데모 버전은 검사 사이트에서만 사용하고 재실행기에는 전달하지 않습니다.', flush=True)
        uvicorn.run(create_app(), host='127.0.0.1', port=port)
    finally:
        for server in [buggy, fixed]:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve-demo', action='store_true')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if args.serve_demo:
        serve_demo(args.port)
    else:
        unittest.main(argv=['check_replay_api'], verbosity=2)
