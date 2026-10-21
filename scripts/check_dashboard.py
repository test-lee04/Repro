"""실제 브라우저에서 제보 화면과 API의 연결을 모델 없이 검사한다."""

import argparse
import asyncio
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from uuid import uuid4

import uvicorn
from playwright.sync_api import sync_playwright, expect

from app.api.run_service import RunService
from app.main import create_app


class DashboardTests(unittest.TestCase):
    """검사 서버와 실행 대역으로 사용자 화면의 전체 흐름을 확인한다."""

    @classmethod
    def setUpClass(cls):
        """모델 대신 검사 대역을 연결한 로컬 서버와 브라우저를 연다."""
        cls.temp = tempfile.TemporaryDirectory()
        cls.output = Path('artifacts') / f'dashboard-{uuid4().hex}'
        cls.output.mkdir(parents=True, exist_ok=True)

        async def worker(request, run_id, folder, *, answers=None):
            if request.report == '대기 검사':
                await asyncio.Event().wait()
            await asyncio.sleep(0.03)
            needs = request.report == '정보가 부족한 제보' and not answers
            (folder / 'evidence.txt').write_text('브라우저 검사 증거', encoding='utf-8')
            condition = {'condition_id': 'fixture-value', 'target_description': '검사용 입력란'}
            comparison = {'condition_id': 'fixture-value', 'expected_value': '저장한 값',
                          'actual_value': '', 'status': 'unsatisfied', 'reason': '정상 기대 조건을 충족하지 않습니다.'}
            return {
                'workflow_status': 'needs_information' if needs else 'conditions_checked',
                'reproduction_status': 'needs_information' if needs else 'reproduced',
                'questions': ['정상 기대 동작을 알려주세요.'] if needs else [],
                'assessment': {'reason': request.report, 'comparisons': [] if needs else [comparison]},
                'checks': [] if needs else [{'condition': condition, 'comparison': comparison}],
                'execution': {'history': [] if needs else [
                    {'success': True, 'action': {'kind': 'fill', 'element_id': 'fixture-input', 'value': '저장한 값'}}]},
                'test_generation': {'test_status': 'not_generated' if needs else 'generated',
                                    'reason': '추가 답변이 필요합니다.' if needs else '테스트를 생성했습니다. 재실행 전입니다.'},
                'error': None,
            }

        application = create_app(RunService(Path(cls.temp.name), worker))
        cls.socket = socket.socket()
        cls.socket.bind(('127.0.0.1', 0))
        cls.url = f'http://127.0.0.1:{cls.socket.getsockname()[1]}'
        cls.server = uvicorn.Server(uvicorn.Config(application, log_level='error', lifespan='on'))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={'sockets': [cls.socket]}, daemon=True)
        cls.thread.start()
        for _ in range(200):
            if cls.server.started:
                break
            time.sleep(0.01)
        else:
            raise RuntimeError('검사 서버가 시작되지 않았습니다.')
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        """브라우저와 서버를 닫고 검사 자료를 정리한다."""
        cls.browser.close()
        cls.playwright.stop()
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        cls.socket.close()
        cls.temp.cleanup()
        print('화면 검사 증거:', cls.output)

    def setUp(self):
        """검사마다 저장소와 쿠키가 분리된 페이지를 연다."""
        self.context = self.browser.new_context(viewport={'width': 1280, 'height': 1000})
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.goto(self.url)

    def tearDown(self):
        """화면의 스크립트 오류가 없는지 확인하고 페이지를 닫는다."""
        self.context.close()
        self.assertEqual(self.errors, [])

    def submit(self, report='저장 후 값이 사라집니다.'):
        """대상 URL과 제보를 실제 입력 화면으로 제출한다."""
        self.page.get_by_label('대상 사이트 URL').fill('http://example.test/')
        self.page.get_by_label('문제 상황과 기대 동작').fill(report)
        self.page.get_by_role('button', name='재현 시도 시작').click()

    def test_submit_result_download(self):
        """제보 접수 후 판정, 비교값, 행동과 파일 다운로드를 확인한다."""
        self.submit()
        expect(self.page.locator('#verdict')).to_have_text('제보 현상 재현')
        expect(self.page.locator('#comparison-list')).to_contain_text('정상 기대값: "저장한 값"')
        expect(self.page.locator('#comparison-list')).to_contain_text('실제 값: ""')
        expect(self.page.locator('#test-status')).to_have_text('생성 완료')
        self.page.get_by_text('실행한 행동', exact=True).click()
        expect(self.page.locator('#action-list')).to_contain_text('입력 / fixture-input')
        with self.page.expect_download() as event:
            self.page.get_by_role('link', name='evidence.txt', exact=True).click()
        download = event.value
        self.assertEqual(Path(download.path()).read_text(), '브라우저 검사 증거')
        self.page.screenshot(path=str(self.output / 'result.png'), full_page=True)

    def test_answers_and_previous_round(self):
        """추가 답변으로 새 회차를 실행하고 이전 결과로 이동한다."""
        self.submit('정보가 부족한 제보')
        expect(self.page.locator('#questions-card')).to_be_visible()
        parent = self.page.locator('#current-id').input_value()
        self.page.get_by_label('정상 기대 동작을 알려주세요.').fill('저장한 값이 유지돼야 합니다.')
        self.page.get_by_role('button', name='답변 제출하고 다시 검사').click()
        expect(self.page.locator('#verdict')).to_have_text('제보 현상 재현')
        child = self.page.locator('#current-id').input_value()
        self.assertNotEqual(parent, child)
        self.page.get_by_role('button', name='이전 검사 회차').click()
        expect(self.page.get_by_role('button', name='다음 검사 회차')).to_be_visible()
        expect(self.page.locator('#questions-card')).to_be_hidden()
        expect(self.page.locator('#verdict')).to_have_text('추가 정보 필요')
        self.page.get_by_role('button', name='다음 검사 회차').click()
        expect(self.page.locator('#verdict')).to_have_text('제보 현상 재현')

    def test_reload_and_lookup(self):
        """새로고침과 식별자 조회로 같은 결과를 복원한다."""
        self.submit()
        expect(self.page.locator('#verdict')).to_have_text('제보 현상 재현')
        run_id = self.page.locator('#current-id').input_value()
        self.page.reload()
        expect(self.page.locator('#verdict')).to_have_text('제보 현상 재현')
        expect(self.page.locator('#current-id')).to_have_value(run_id)
        self.page.get_by_label('작업 식별자', exact=True).fill(run_id)
        self.page.get_by_role('button', name='작업 불러오기').click()
        expect(self.page.locator('#verdict')).to_have_text('제보 현상 재현')

    def test_cancel(self):
        """검사 중인 작업을 화면에서 취소한다."""
        self.submit('대기 검사')
        expect(self.page.locator('#job-status')).to_have_text('검사 중')
        self.page.get_by_role('button', name='실행 취소').click()
        expect(self.page.locator('#job-status')).to_have_text('취소됨')
        expect(self.page.get_by_role('button', name='재현 시도 시작')).to_be_enabled()

    def test_unknown_run(self):
        """없는 작업의 조회 오류를 화면에 표시한다."""
        self.page.get_by_label('작업 식별자', exact=True).fill('a' * 32)
        self.page.get_by_role('button', name='작업 불러오기').click()
        expect(self.page.locator('#notice')).to_contain_text('작업 또는 파일을 찾을 수 없습니다.')

    def test_render_text_safely(self):
        """제보와 결과 문구를 화면 코드가 아닌 문자로 표시한다."""
        text = '<img src=x onerror="window.fixtureExecuted=true">'
        self.submit(text)
        expect(self.page.locator('#verdict-reason')).to_have_text(text)
        self.assertIsNone(self.page.evaluate('window.fixtureExecuted'))
        self.assertEqual(self.page.locator('#verdict-reason img').count(), 0)

    def test_answer_failure_preserves_input(self):
        """답변 제출이 실패해도 입력을 유지하고 다시 제출할 수 있다."""
        self.submit('정보가 부족한 제보')
        expect(self.page.locator('#questions-card')).to_be_visible()
        self.page.get_by_label('정상 기대 동작을 알려주세요.').fill('유지해야 합니다.')
        pattern = '**/api/runs/*/answers'
        self.page.route(pattern, lambda route: route.fulfill(status=409, content_type='application/json',
                                                            body=json.dumps({'detail': '검사용 제출 오류'})))
        self.page.get_by_role('button', name='답변 제출하고 다시 검사').click()
        expect(self.page.locator('#notice')).to_contain_text('검사용 제출 오류')
        expect(self.page.get_by_label('정상 기대 동작을 알려주세요.')).to_have_value('유지해야 합니다.')
        self.page.unroute(pattern)
        self.page.get_by_role('button', name='답변 제출하고 다시 검사').click()
        expect(self.page.locator('#verdict')).to_have_text('제보 현상 재현')

    def test_mobile_layout(self):
        """좁은 화면에서도 입력과 결과가 화면 너비를 벗어나지 않는다."""
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.submit()
        expect(self.page.locator('#verdict')).to_have_text('제보 현상 재현')
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
        self.page.screenshot(path=str(self.output / 'mobile.png'), full_page=True)


def serve_demo(version, port):
    """실제 Agent 화면과 별도의 메모 검사 사이트를 함께 실행한다.

    Args:
        version: 메모 검사 사이트의 buggy 또는 fixed 버전.
        port: 사용자 화면을 제공할 로컬 서버 포트.

    Notes:
        화면에서 제보를 제출해야 실제 모델을 호출한다.
        데모 버전은 검사 사이트에서만 사용한다.
    """
    from scripts.check_reproduction_workflow import start_demo

    server, target_url = start_demo(version)
    try:
        print(f'사용자 화면: http://127.0.0.1:{port}', flush=True)
        print(f'입력할 대상 사이트 URL: {target_url}', flush=True)
        print('검사용 제보: 문서 제목에 주간 계획을 입력하고 저장한 뒤 새로고침하면 제목이 비어 있습니다. '
              '저장한 제목은 새로고침 후에도 주간 계획으로 유지돼야 합니다.', flush=True)
        print('추가 질문 확인용 제보: 저장 기능이 이상합니다.', flush=True)
        print('종료하려면 이 터미널에서 Ctrl+C를 누르세요.', flush=True)
        uvicorn.run(create_app(), host='127.0.0.1', port=port, log_level='info')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve-demo', choices=['buggy', 'fixed'])
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if args.serve_demo:
        serve_demo(args.serve_demo, args.port)
    else:
        unittest.main(argv=['check_dashboard'], verbosity=2)
