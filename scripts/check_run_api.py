"""모델과 외부 사이트 없이 제보 작업 API의 계약과 저장 동작을 검사한다."""

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


class RunApiTests(unittest.TestCase):
    """실행 대역을 주입해 요청, 조회, 취소와 파일 접근을 확인한다."""

    def setUp(self):
        """독립된 저장 위치와 실제 API 앱을 준비한다."""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.received = []

        async def worker(request, run_id, folder):
            self.received.append(request.model_dump(mode='json'))
            if request.report == '기다리는 작업':
                await asyncio.Event().wait()
            if request.report == '실행 오류':
                raise ValueError('검사용 오류')
            (folder / 'evidence.txt').write_text('실행 증거', encoding='utf-8')
            status = 'needs_information' if request.report == '정보 부족' else 'conditions_checked'
            return {'workflow_status': status, 'reproduction_status': 'needs_information'
                    if status == 'needs_information' else 'reproduced',
                    'questions': ['어떤 결과를 기대하나요?'] if status == 'needs_information' else [],
                    'assessment': {'reason': '검사용 판정'}, 'error': None,
                    'test_generation': {'test_status': 'generated'}}

        self.service = RunService(self.root, worker=worker)
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()

    def tearDown(self):
        """서버의 실행 작업과 임시 자료를 정리한다."""
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def submit(self, report='저장 후 내용이 사라집니다.'):
        """대상 URL과 제보로 작업을 접수한다."""
        response = self.client.post('/api/runs', json={
            'report': report, 'target': {'start_url': 'http://example.test/',
                                      'public_spec': '저장한 내용이 유지돼야 합니다.'}})
        self.assertEqual(response.status_code, 202)
        return response.json()['run_id']

    def wait(self, run_id, expected):
        """검사 대상의 종료 상태를 짧은 제한 시간 동안 확인한다."""
        for _ in range(200):
            state = self.client.get(f'/api/runs/{run_id}').json()
            if state['status'] in expected:
                return state
            time.sleep(0.005)
        self.fail(f'예상 상태에 도달하지 못했습니다: {state}')

    def test_submit_and_result(self):
        """접수한 제보와 요구사항을 전달하고 결과를 조회한다."""
        run_id = self.submit()
        self.wait(run_id, {'completed'})
        result = self.client.get(f'/api/runs/{run_id}/result')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['reproduction_status'], 'reproduced')
        self.assertEqual(self.received[0]['target']['public_spec'], '저장한 내용이 유지돼야 합니다.')

    def test_invalid_request(self):
        """대상이 없거나 잘못된 주소인 요청을 거부한다."""
        for payload in [{'report': '문제'}, {'report': '문제', 'target': {'start_url': 'file:///tmp/a'}}]:
            self.assertEqual(self.client.post('/api/runs', json=payload).status_code, 422)

    def test_legacy_mode_rejected(self):
        """아직 연결하지 않은 실행 방식을 명시적으로 거부한다."""
        self.assertEqual(self.client.post('/api/runs', json={
            'report': '문제', 'target_id': 'cart'}).status_code, 422)
        self.assertEqual(self.client.post('/api/runs', json={
            'report': '문제', 'target': {'start_url': 'http://example.test'},
            'mode': 'one_shot'}).status_code, 422)

    def test_unknown_run(self):
        """없는 작업은 찾을 수 없음으로 응답한다."""
        self.assertEqual(self.client.get('/api/runs/unknown').status_code, 404)

    def test_pending_result_and_cancel(self):
        """실행 중 결과는 보류하고 취소 후 상태를 기록한다."""
        run_id = self.submit('기다리는 작업')
        self.wait(run_id, {'running'})
        self.assertEqual(self.client.get(f'/api/runs/{run_id}/result').status_code, 409)
        self.assertEqual(self.client.post(f'/api/runs/{run_id}/cancel').status_code, 202)
        self.wait(run_id, {'cancelled'})

    def test_queued_cancel(self):
        """다른 작업이 실행 중일 때 대기 작업도 취소한다."""
        first = self.submit('기다리는 작업')
        self.wait(first, {'running'})
        second = self.submit()
        self.assertEqual(self.client.post(f'/api/runs/{second}/cancel').status_code, 202)
        self.wait(second, {'cancelled'})
        self.assertEqual(len(self.received), 1)

    def test_worker_error(self):
        """브라우저 준비 등 실행 전 오류도 실패 상태로 남긴다."""
        run_id = self.submit('실행 오류')
        state = self.wait(run_id, {'failed'})
        self.assertEqual(state['error']['type'], 'ValueError')
        self.assertEqual(self.client.get(f'/api/runs/{run_id}/result').status_code, 409)

    def test_questions(self):
        """추가 질문이 필요한 실행의 상태와 질문을 제공한다."""
        run_id = self.submit('정보 부족')
        self.wait(run_id, {'needs_information'})
        result = self.client.get(f'/api/runs/{run_id}/result').json()
        self.assertTrue(result['questions'])
        self.assertEqual(self.client.post(f'/api/runs/{run_id}/answers', json={'answers': {}}).status_code, 422)

    def test_download(self):
        """목록에 있는 파일을 다운로드하고 없는 식별자는 거부한다."""
        run_id = self.submit()
        self.wait(run_id, {'completed'})
        items = self.client.get(f'/api/runs/{run_id}/artifacts').json()['artifacts']
        self.assertNotIn('request.json', [item['name'] for item in items])
        evidence = next(item for item in items if item['name'] == 'evidence.txt')
        response = self.client.get(evidence['download_url'])
        self.assertEqual(response.text, '실행 증거')
        self.assertIn('attachment', response.headers['content-disposition'])
        self.assertEqual(self.client.get(f'/api/runs/{run_id}/artifacts/unknown').status_code, 404)

    def test_external_symlink_excluded(self):
        """결과 폴더 밖을 가리키는 연결 파일을 노출하지 않는다."""
        run_id = self.submit()
        self.wait(run_id, {'completed'})
        outside = self.root / 'outside.txt'
        outside.write_text('외부 자료')
        (self.service.folder(run_id) / 'linked.txt').symlink_to(outside)
        items = self.client.get(f'/api/runs/{run_id}/artifacts').json()['artifacts']
        self.assertNotIn('linked.txt', [item['name'] for item in items])

    def test_restore(self):
        """서버를 다시 만들면 저장된 완료 결과를 조회할 수 있다."""
        run_id = self.submit()
        self.wait(run_id, {'completed'})
        restored = RunService(self.root)
        self.assertEqual(restored.get(run_id)['status'], 'completed')
        self.assertEqual(restored.result(run_id)['reproduction_status'], 'reproduced')

    def test_interrupted_restore(self):
        """이전 서버에서 실행 중이던 기록은 재시작 시 중단으로 표시한다."""
        folder = self.root / 'api-run-saved'
        folder.mkdir()
        (folder / 'api_state.json').write_text(json.dumps({'run_id': 'saved', 'status': 'running'}))
        restored = RunService(self.root)
        self.assertEqual(restored.get('saved')['status'], 'interrupted')

    def test_health(self):
        """현재 제공하는 기능과 아직 연결하지 않은 기능을 구분한다."""
        state = self.client.get('/health').json()
        self.assertEqual(state['stage'], 'answer_api')
        self.assertTrue(state['answers_implemented'])


def live_demo(version):
    """실제 모델과 브라우저를 API에 연결해 검사용 화면을 실행한다.

    Args:
        version: 검사 서버에서만 사용하는 buggy 또는 fixed.

    Notes:
        데모 버전은 Agent의 입력이나 판단 기준으로 전달하지 않는다.
        이 검사는 API 요청을 앱 내부로 전달하므로 별도 API 서버가 필요 없다.
    """
    from scripts.check_reproduction_workflow import start_demo

    server, url = start_demo(version)
    try:
        with TestClient(create_app()) as client:
            response = client.post('/api/runs', json={
                'report': '문서 제목에 주간 계획을 입력하고 저장한 뒤 새로고침하면 제목이 비어 있습니다. '
                          '저장한 제목은 새로고침 후에도 주간 계획으로 유지돼야 합니다.',
                'target': {'start_url': url},
            })
            response.raise_for_status()
            job = response.json()
            run_id = job['run_id']
            print('접수한 작업:', run_id)
            deadline = time.monotonic() + 360
            previous = None
            while time.monotonic() < deadline:
                state_response = client.get(f'/api/runs/{run_id}')
                state_response.raise_for_status()
                state = state_response.json()
                if state['status'] != previous:
                    print('작업 상태:', state['status'])
                    previous = state['status']
                if state['status'] not in {'queued', 'running', 'cancelling'}:
                    break
                time.sleep(0.5)
            else:
                client.post(f'/api/runs/{run_id}/cancel')
                raise TimeoutError('API 검사 제한 시간에 도달해 취소를 요청했습니다.')
            print('작업 정보:', json.dumps(state, ensure_ascii=False, indent=2))
            result_response = client.get(job['result_url'])
            result_response.raise_for_status()
            result = result_response.json()
            print('최종 재현 상태:', result['reproduction_status'])
            print('판정:', json.dumps(result.get('assessment'), ensure_ascii=False, indent=2))
            print('테스트 생성:', json.dumps(result.get('test_generation'), ensure_ascii=False, indent=2))
            if result.get('questions'):
                print('추가 질문:', result['questions'])
            files_response = client.get(job['artifacts_url'])
            files_response.raise_for_status()
            files = files_response.json()['artifacts']
            print('조회 가능한 결과 파일:', len(files))
            workflow_file = next(item for item in files if item['name'] == 'workflow_result.json')
            downloaded = client.get(workflow_file['download_url'])
            downloaded.raise_for_status()
            assert downloaded.json() == result, '다운로드한 결과와 API 결과가 다릅니다.'
            expected = 'reproduced' if version == 'buggy' else 'not_reproduced'
            assert result['reproduction_status'] == expected, '데모의 예상 재현 상태와 다릅니다.'
            assert result['test_generation']['test_status'] == 'generated', '테스트 생성 결과를 확인하세요.'
            print('PASS: API 접수, 실제 재현 흐름, 최종 결과 조회와 파일 다운로드')
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
        unittest.main(argv=['check_run_api'], verbosity=2)
