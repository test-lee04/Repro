"""실제 화면과 브라우저 재실행을 연결해 수정 검증 표시를 검사한다."""

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
from scripts.check_replay_api import make_result
from scripts.check_reproduction_workflow import start_demo


class ReplayDashboardTests(unittest.TestCase):
    """생성된 검사 자료로 실제 재실행기를 사용하며 모델은 호출하지 않는다."""

    @classmethod
    def setUpClass(cls):
        """원래·수정 검사 사이트와 사용자 화면 서버를 연다."""
        cls.temp = tempfile.TemporaryDirectory()
        cls.output = Path('artifacts') / f'replay-dashboard-{uuid4().hex}'
        cls.output.mkdir(parents=True, exist_ok=True)
        cls.original_server, cls.original_url = start_demo('buggy')
        cls.fix_server, cls.fix_url = start_demo('fixed')

        async def worker(request, run_id, folder):
            return make_result(folder)

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
            raise RuntimeError('검사 서버를 시작하지 못했습니다.')
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        """모든 브라우저와 검사 서버를 정리한다."""
        cls.browser.close()
        cls.playwright.stop()
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        cls.socket.close()
        for server in [cls.original_server, cls.fix_server]:
            server.shutdown()
            server.server_close()
        cls.temp.cleanup()
        print('화면 검사 증거:', cls.output)

    def setUp(self):
        """화면에서 재실행할 원래 작업과 생성 테스트를 준비한다."""
        self.context = self.browser.new_context(viewport={'width': 1280, 'height': 1100})
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.goto(self.url)
        self.page.get_by_label('대상 사이트 URL', exact=True).fill(self.original_url)
        self.page.get_by_label('문제 상황과 기대 동작').fill('검사용 원래 실행 기록')
        self.page.get_by_role('button', name='재현 시도 시작').click()
        expect(self.page.locator('#replay-card')).to_be_visible()
        expect(self.page.locator('#verdict')).to_have_text('제보 현상 재현')

    def tearDown(self):
        """스크립트 오류가 없는지 확인하고 화면을 닫는다."""
        self.context.close()
        self.assertEqual(self.errors, [])

    def replay(self, purpose, url):
        """환경 역할과 실제 URL을 화면에서 선택하고 제출한다."""
        self.page.get_by_label('재실행 대상 URL').fill(url)
        self.page.get_by_label('재실행 환경 역할').select_option(purpose)
        self.page.get_by_role('button', name='동일한 테스트 재실행').click()

    def test_original_then_fix(self):
        """동일한 테스트로 원래 환경 실패와 수정 환경 통과를 화면에서 확인한다."""
        self.replay('original', self.original_url)
        expect(self.page.locator('#test-status')).to_have_text('수정 검증 대기', timeout=20000)
        expect(self.page.locator('#replay-history')).to_contain_text('원래 환경 / 조건 실패 확인')
        self.replay('fix', self.fix_url)
        expect(self.page.locator('#test-status')).to_have_text('수정 검증 완료', timeout=20000)
        expect(self.page.locator('#replay-history')).to_contain_text('수정 환경 / 조건 통과')
        expect(self.page.locator('#verdict')).to_have_text('제보 현상 재현')
        expect(self.page.get_by_role('link', name='replay_history.json', exact=True)).to_be_visible()
        self.page.screenshot(path=str(self.output / 'validated.png'), full_page=True)

    def test_fix_alone(self):
        """수정 환경 통과만으로 화면에 수정 검증 완료를 표시하지 않는다."""
        self.replay('fix', self.fix_url)
        expect(self.page.locator('#replay-state')).to_have_text('조건 통과', timeout=20000)
        expect(self.page.locator('#test-status')).to_have_text('생성 완료')
        expect(self.page.locator('#test-reason')).to_contain_text('원래 환경의 조건 실패를 아직 확인하지 못했습니다.')

    def test_reload_during_replay(self):
        """실행 중 화면을 새로고침해도 진행 상태와 결과를 다시 조회한다."""
        self.replay('original', self.original_url)
        expect(self.page.locator('#replay-state')).to_have_text('검사 중', timeout=10000)
        self.page.reload()
        expect(self.page.locator('#test-status')).to_have_text('수정 검증 대기', timeout=20000)
        expect(self.page.locator('#replay-history li')).to_have_count(1)

    def test_replay_error(self):
        """접속 오류를 수정 검증 완료로 오판하지 않고 원래 판정도 유지한다."""
        self.replay('original', 'http://127.0.0.1:1/')
        expect(self.page.locator('#test-status')).to_have_text('실행 오류', timeout=20000)
        expect(self.page.locator('#replay-state')).to_have_text('실행 오류')
        expect(self.page.locator('#verdict')).to_have_text('제보 현상 재현')


if __name__ == '__main__':
    unittest.main(verbosity=2)
