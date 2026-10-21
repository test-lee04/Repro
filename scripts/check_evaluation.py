"""평가 집계, 실제 호출 계측과 평가 화면의 정답을 모델 없이 검사한다."""

import asyncio
import os
import io
from contextlib import redirect_stdout
from types import SimpleNamespace
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from eval.fixtures import CASES, FixtureServer, dataset_fingerprint
from eval.metered_client import MeteredClient
from eval.run_eval import run_trial, evaluate
from eval.summarize import rate, summarize, write_reports


def sample_row(actual='reproduced', expected='reproduced'):
    """집계 검사용 한 시도의 원시 측정값을 만든다."""
    return {'trial_id': 'trial-1', 'case_id': 'fixture', 'variant': 'buggy', 'repeat': 1,
            'actual_status': actual, 'expected_status': expected, 'elapsed_seconds': 10,
            'test_generated': True, 'test_status': 'generated', 'verification_attempted': False,
            'usage': {'model_calls': 1, 'model_errors': 0, 'input_tokens': 10, 'output_tokens': 5,
                      'input_tokens_known_subtotal': 10, 'output_tokens_known_subtotal': 5,
                      'input_tokens_known_calls': 1, 'output_tokens_known_calls': 1}}


def manifest(rows):
    """계획과 실제 시도 수를 구분한 평가 자료를 만든다."""
    return {'rows': rows, 'planned_trials': len(rows), 'status': 'completed',
            'dataset_version': '검사용', 'dataset_fingerprint': '검사용 식별값'}


class SummaryTests(unittest.TestCase):
    """좋은 결과만 남기는 집계와 누락값의 임의 변환을 방지한다."""

    def test_errors_in_denominator(self):
        """실행 오류와 보류도 버그 탐지의 분모에서 제외하지 않는다."""
        rows = [sample_row(), sample_row('environment_error'), sample_row('inconclusive')]
        for index, row in enumerate(rows):
            row['trial_id'] = str(index)
        result = summarize(manifest(rows))
        self.assertEqual(result['bug_detection'], rate(1, 3))
        self.assertEqual(result['exact_match'], rate(1, 3))

    def test_false_positive(self):
        """정상 환경의 재현 판정만 오탐으로 집계한다."""
        result = summarize(manifest([sample_row('reproduced', 'not_reproduced')]))
        self.assertEqual(result['false_positive'], rate(1, 1))
        self.assertEqual(result['normal_confirmation'], rate(0, 1))

    def test_clarification_separate(self):
        """정보 부족 정답은 버그·정상 판정의 분모에 넣지 않는다."""
        result = summarize(manifest([sample_row('needs_information', 'needs_information')]))
        self.assertEqual(result['clarification'], rate(1, 1))
        self.assertEqual(result['bug_detection']['total'], 0)

    def test_unmeasured(self):
        """시도하지 않은 수정 검증은 성공률 0이 아닌 미측정이다."""
        self.assertIsNone(summarize(manifest([sample_row()]))['fix_validation']['percent'])
        self.assertIsNone(summarize(manifest([]))['exact_match']['percent'])

    def test_duplicate_rejected(self):
        """같은 평가 행을 중복해 성공 수를 늘릴 수 없다."""
        with self.assertRaises(ValueError):
            summarize(manifest([sample_row(), sample_row()]))

    def test_missing_tokens(self):
        """사용량을 모르는 호출이 있으면 전체 토큰 합계를 확정하지 않는다."""
        row = sample_row()
        row['usage'].update(input_tokens=None, input_tokens_known_subtotal=0, input_tokens_known_calls=0)
        result = summarize(manifest([row]))
        self.assertIsNone(result['input_tokens'])
        self.assertEqual(result['input_tokens_known_calls'], 0)

    def test_output_reports(self):
        """원시 기록에서 JSON, CSV와 읽을 수 있는 결과표를 만든다."""
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / 'evaluation.json').write_text(json.dumps(manifest([sample_row()])))
            write_reports(folder)
            for name in ['summary.json', 'summary.md', 'results.csv']:
                self.assertTrue((folder / name).is_file())
            text = (folder / 'summary.md').read_text()
            self.assertIn('1/1 (100.0%)', text)
            self.assertIn('수정 검증 완료 | 미측정', text)


class MeterTests(unittest.IsolatedAsyncioTestCase):
    """모델 호출의 실패와 누락 사용량도 정확히 계측한다."""

    async def test_calls_and_usage(self):
        """실제 호출마다 한 번만 세고 응답 내용은 바꾸지 않는다."""
        class Client:
            async def generate(self, **kwargs):
                return {'content': '{}', 'usage': {'input_tokens': 12, 'output_tokens': 4, 'model': 'fixture'}}
        with tempfile.TemporaryDirectory() as temporary:
            meter = MeteredClient(Client(), Path(temporary) / 'calls.json')
            response = await meter.generate([], {})
            await meter.generate([], {})
            self.assertEqual(response['content'], '{}')
            self.assertEqual(meter.summary()['model_calls'], 2)
            self.assertEqual(meter.summary()['input_tokens'], 24)

    async def test_failed_call(self):
        """예외가 난 요청도 호출 횟수에 포함하고 토큰은 추측하지 않는다."""
        class Client:
            async def generate(self, **kwargs):
                raise RuntimeError('검사용 연결 오류')
        with tempfile.TemporaryDirectory() as temporary:
            meter = MeteredClient(Client(), Path(temporary) / 'calls.json')
            with self.assertRaises(RuntimeError):
                await meter.generate([], {})
            summary = meter.summary()
            self.assertEqual(summary['model_calls'], 1)
            self.assertEqual(summary['model_errors'], 1)
            self.assertIsNone(summary['input_tokens'])

    async def test_cancelled_call(self):
        """취소된 호출도 저장한 뒤 취소를 호출자에게 전달한다."""
        class Client:
            async def generate(self, **kwargs):
                raise asyncio.CancelledError()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'calls.json'
            meter = MeteredClient(Client(), path)
            with self.assertRaises(asyncio.CancelledError):
                await meter.generate([], {})
            self.assertEqual(meter.summary()['model_calls'], 1)
            self.assertTrue(path.exists())

    async def test_agent_input_has_no_truth(self):
        """Agent 호출에는 제보와 대상만 전달하고 정답·환경 이름을 주지 않는다."""
        received = []
        class Server:
            def url(self, case_id, variant):
                return 'http://127.0.0.1:8000/0123456789abcdef'
        async def execute(request, run_id, folder, **kwargs):
            received.append(request.model_dump(mode='json'))
            return {'reproduction_status': 'reproduced', 'workflow_status': 'conditions_checked'}
        with tempfile.TemporaryDirectory() as temporary:
            with patch('eval.run_eval.build_model_client', return_value=object()), patch('eval.run_eval.execute_workflow', execute):
                row, cancelled = await run_trial(CASES[0], 'buggy', 1, Server(), Path(temporary), 1, False)
            self.assertFalse(cancelled)
            self.assertEqual(received[0]['report'], CASES[0].report)
            text = json.dumps(received)
            for forbidden in ['expected_status', 'case_id', 'buggy', 'fixed']:
                self.assertNotIn(forbidden, text)
            self.assertEqual(row['expected_status'], 'reproduced')

    async def test_full_collection_without_model(self):
        """평가 실행기가 모든 시도를 모아 원본과 보고서를 함께 저장한다."""
        class Server:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def url(self, *args):
                return 'http://127.0.0.1:8000/0123456789abcdef'
        async def execute(*args, **kwargs):
            return {'reproduction_status': 'inconclusive', 'workflow_status': 'conditions_checked'}
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            try:
                os.chdir(temporary)
                with patch('eval.run_eval.FixtureServer', Server), patch('eval.run_eval.build_model_client', return_value=object()), patch('eval.run_eval.execute_workflow', execute), redirect_stdout(io.StringIO()):
                    await evaluate(SimpleNamespace(case='all', repeats=1, timeout=1, verify_tests=False))
                path = next(Path('artifacts').glob('evaluation-*/evaluation.json'))
                data = json.loads(path.read_text())
                self.assertEqual(data['planned_trials'], 7)
                self.assertEqual(len(data['rows']), 7)
                self.assertEqual(data['status'], 'completed')
                self.assertTrue((path.parent / 'summary.md').is_file())
                self.assertEqual(json.loads((path.parent / 'summary.json').read_text())['exact_match']['count'], 0)
            finally:
                os.chdir(previous)

    async def test_trial_exception_saved(self):
        """실행 준비 오류가 생겨도 평가 행을 저장한다."""
        class Server:
            def url(self, *args):
                return 'http://127.0.0.1:8000/0123456789abcdef'
        with tempfile.TemporaryDirectory() as temporary:
            with patch('eval.run_eval.build_model_client', side_effect=RuntimeError('검사용 준비 오류')):
                row, cancelled = await run_trial(CASES[0], 'buggy', 1, Server(), Path(temporary), 1, False)
            self.assertEqual(row['actual_status'], 'environment_error')
            self.assertTrue((Path(row['artifact_dir']) / 'evaluation_row.json').is_file())


class FixtureTests(unittest.TestCase):
    """평가 정답이 로컬 사이트의 실제 동작과 일치하는지 확인한다."""

    @classmethod
    def setUpClass(cls):
        """서버와 실제 브라우저를 시작한다."""
        cls.server = FixtureServer().__enter__()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        """평가 자료 검사에 사용한 브라우저와 서버를 정리한다."""
        cls.browser.close()
        cls.playwright.stop()
        cls.server.__exit__()

    def check_page(self, case_id, action):
        """버그·정상 환경을 각각 독립된 브라우저 저장소에서 확인한다."""
        for variant in ['buggy', 'fixed']:
            with self.browser.new_context() as context:
                page = context.new_page()
                page.goto(self.server.url(case_id, variant))
                action(page, variant)

    def test_note(self):
        """제목 저장과 새로고침의 버그·정상 정답을 확인한다."""
        def action(page, variant):
            page.get_by_label('문서 제목').fill('주간 계획')
            page.get_by_role('button', name='저장', exact=True).click()
            page.reload()
            self.assertEqual(page.get_by_label('문서 제목').input_value(), '주간 계획' if variant == 'fixed' else '')
        self.check_page('note', action)

    def test_search(self):
        """검색 결과 목록의 버그·정상 정답을 확인한다."""
        def action(page, variant):
            page.get_by_label('검색어').fill('회의록')
            page.get_by_role('button', name='검색', exact=True).click()
            self.assertEqual('회의록' in page.locator('#results').inner_text(), variant == 'fixed')
        self.check_page('search', action)

    def test_validation(self):
        """빈 이메일로 가입할 때 안내 문구의 정답을 확인한다."""
        def action(page, variant):
            page.get_by_role('button', name='가입', exact=True).click()
            self.assertEqual(page.locator('#result').inner_text(), '이메일을 입력해 주세요.' if variant == 'fixed' else '가입 완료')
        self.check_page('validation', action)

    def test_opaque_urls_and_fingerprint(self):
        """주소에 환경 정답을 넣지 않고 자료 식별값이 안정적인지 확인한다."""
        for case in CASES:
            for variant in ['buggy', 'fixed']:
                url = self.server.url(case.case_id, variant)
                self.assertNotIn('buggy', url)
                self.assertNotIn('fixed', url)
        self.assertEqual(dataset_fingerprint(), dataset_fingerprint())
        self.assertEqual(len(dataset_fingerprint()), 64)


if __name__ == '__main__':
    unittest.main(verbosity=2)
