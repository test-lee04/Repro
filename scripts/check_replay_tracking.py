"""회귀 테스트의 재실행 이력과 원래·수정 환경의 검증 상태를 검사한다."""

import argparse
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.testing.compiler import RecordedTestCompiler, UnsupportedCompilation
from app.testing.replay import RecordedReplayRunner, write_json
from scripts.check_test_compiler import fixture_run


class TrackingTests(unittest.IsolatedAsyncioTestCase):
    """검사 대역으로 상태 갱신과 잘못된 검증 완료 표시를 확인한다."""

    async def asyncSetUp(self):
        """실제 사용자 기록과 분리한 임시 실행 기록을 준비한다."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run_record = fixture_run()
        compiler = RecordedTestCompiler()
        self.run_record['test_generation'] = compiler.export(
            compiler.compile_run(self.run_record), self.root / 'regression')
        write_json(self.root / 'workflow_result.json', self.run_record)

    async def asyncTearDown(self):
        """임시 검사 자료를 정리한다."""
        self.temporary.cleanup()

    def runner(self, passed=False, incomplete=False, failure=False):
        """행동과 조건 결과를 반환하는 실행 대역을 만든다."""
        async def execute(case, url, folder, result, timeout):
            if failure:
                raise RuntimeError('검사용 실행 오류')
            result['actions'] = [{'action': item, 'success': True} for item in case['actions']]
            result['assertions'] = []
            for item in case['assertions']:
                condition = item['condition']
                result['assertions'].append({
                    **{key: condition[key] for key in
                       ['condition_id', 'kind', 'operator', 'expected_value']},
                    'actual_value': condition['expected_value'] if passed else '',
                    'passed': passed,
                })
            result['status'] = 'passed' if passed else 'assertion_failed'
            if incomplete:
                result['actions'] = []
        return RecordedReplayRunner(executor=execute)

    async def replay(self, purpose, passed=False, **kwargs):
        """검사 환경에서 한 번 재실행하고 갱신된 상태를 반환한다."""
        result = await self.runner(passed, **kwargs).run(
            self.root, 'http://127.0.0.1:8000/', purpose=purpose)
        return result['test_generation']

    async def test_original_failure(self):
        """원래 환경 실패 이후에는 수정 환경 검증을 기다린다."""
        state = await self.replay('original')
        self.assertEqual(state['test_status'], 'awaiting_fix_validation')

    async def test_fix_alone(self):
        """수정 환경 통과만으로 검증 완료를 표시하지 않는다."""
        state = await self.replay('fix', True)
        self.assertEqual(state['validation_status'], 'baseline_missing')

    async def test_ordered_validation(self):
        """동일한 테스트의 원래 환경 실패 후 수정 환경 통과를 확인한다."""
        await self.replay('original')
        state = await self.replay('fix', True)
        self.assertEqual(state['test_status'], 'validated')

    async def test_wrong_order(self):
        """원래 환경 실패보다 앞선 통과 결과를 재사용하지 않는다."""
        await self.replay('fix', True)
        state = await self.replay('original')
        self.assertEqual(state['test_status'], 'awaiting_fix_validation')

    async def test_fix_failure(self):
        """수정 환경에서도 조건이 실패하면 검증을 완료하지 않는다."""
        await self.replay('original')
        state = await self.replay('fix')
        self.assertEqual(state['validation_status'], 'fix_failed')

    async def test_error(self):
        """실행 오류를 조건 실패나 수정 검증 완료로 바꾸지 않는다."""
        await self.replay('original')
        state = await self.replay('fix', failure=True)
        self.assertEqual(state['test_status'], 'environment_error')

    async def test_incomplete_success(self):
        """행동 기록이 빠진 통과 응답은 실행 오류로 기록한다."""
        state = await self.replay('fix', True, incomplete=True)
        self.assertEqual(state['replay_status'], 'replay_error')

    async def test_changed_fingerprint(self):
        """자료가 바뀌면 이전 테스트의 실패 기록을 이어 쓰지 않는다."""
        await self.replay('original')
        path = self.root / 'regression' / 'case.json'
        case = json.loads(path.read_text())
        case['note'] = '검사 자료 변경'
        write_json(path, case)
        state = await self.replay('fix', True)
        self.assertEqual(state['validation_status'], 'baseline_missing')

    async def test_changed_expectation(self):
        """원래 기록과 다른 기대값으로 바꾼 테스트를 거부한다."""
        path = self.root / 'regression' / 'case.json'
        case = json.loads(path.read_text())
        case['assertions'][0]['condition']['expected_value'] = '변경한 기대값'
        write_json(path, case)
        with self.assertRaises(UnsupportedCompilation):
            await self.replay('fix', True)

    async def test_changed_script(self):
        """고정 코드와 다른 실행 파일은 재생성하도록 거부한다."""
        (self.root / 'regression' / 'test_regression.py').write_text('# 변경')
        with self.assertRaises(UnsupportedCompilation):
            await self.replay('original')

    async def test_saved_records(self):
        """원래 판정은 보존하고 생성 상태와 이력을 함께 저장한다."""
        state = await self.replay('original')
        saved = json.loads((self.root / 'workflow_result.json').read_text())
        generation = json.loads((self.root / 'test_generation.json').read_text())
        history = json.loads((self.root / 'replay_history.json').read_text())
        self.assertEqual(saved['assessment'], self.run_record['assessment'])
        self.assertEqual(saved['reproduction_status'], self.run_record['reproduction_status'])
        self.assertEqual(saved['test_generation'], generation)
        self.assertEqual(generation, state)
        self.assertEqual(len(history['runs']), 1)
        self.assertTrue(Path(history['runs'][0]['result_path']).is_file())

    async def test_lock(self):
        """같은 실행 기록에 동시에 쓰는 재실행을 거부한다."""
        (self.root / '.replay.lock').write_text('검사 중')
        with self.assertRaises(RuntimeError):
            await self.replay('original')

    async def test_cancellation(self):
        """취소 결과를 저장하고 잠금을 해제한 뒤 취소를 전달한다."""
        async def cancel(*args):
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await RecordedReplayRunner(executor=cancel).run(
                self.root, 'http://127.0.0.1:8000/', purpose='original')
        self.assertFalse((self.root / '.replay.lock').exists())
        history = json.loads((self.root / 'replay_history.json').read_text())
        self.assertEqual(history['runs'][0]['status'], 'replay_error')


async def live(args):
    """기존 실행 폴더의 테스트를 지정한 환경에서 재실행한다."""
    server = None
    try:
        if args.demo:
            from scripts.check_reproduction_workflow import start_demo
            server, url = start_demo(args.demo)
        else:
            url = args.url
        result = await RecordedReplayRunner().run(
            Path(args.run_dir), url, purpose=args.purpose)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print('원래 제보의 재현 판정은 변경하지 않았습니다.')
        print('검증 결과는 이번 대상 환경과 생성한 테스트 조건에 한정됩니다.')
    finally:
        if server:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir')
    target = parser.add_mutually_exclusive_group()
    target.add_argument('--demo', choices=['buggy', 'fixed'])
    target.add_argument('--url')
    parser.add_argument('--purpose', choices=['original', 'fix'])
    args = parser.parse_args()
    if args.run_dir:
        if not args.purpose or not (args.demo or args.url):
            parser.error('재실행에는 --purpose와 --demo 또는 --url이 필요합니다.')
        asyncio.run(live(args))
    else:
        if args.purpose or args.demo or args.url:
            parser.error('재실행에는 --run-dir이 필요합니다.')
        unittest.main(argv=['check_replay_tracking'], verbosity=2)
