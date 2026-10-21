"""여러 평가 화면을 기존 Agent 실행 흐름에 전달하고 실측 결과를 저장한다."""

import argparse
import asyncio
import json
import hashlib
import platform
import math
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from dotenv import load_dotenv

from app.api.run_service import execute_workflow
from app.agent.model_client import build_model_client
from app.schemas import RunRequest
from app.testing.replay import RecordedReplayRunner, write_json
from eval.fixtures import CASES, FIXTURE_VERSION, FixtureServer, dataset_fingerprint
from eval.metered_client import MeteredClient
from eval.summarize import write_reports, format_rate


def expected_status(case, variant):
    """평가기에서만 사용하는 정답이며 Agent 입력에는 포함하지 않는다."""
    return 'needs_information' if case.needs_information else 'reproduced' if variant == 'buggy' else 'not_reproduced'


async def run_trial(case, variant, repeat, server, root, timeout, verify_tests):
    """한 평가 시도를 실행하며 성공·오류·보류를 빠짐없이 기록한다."""
    trial_id = uuid4().hex
    folder = root / trial_id
    folder.mkdir()
    request = RunRequest.model_validate({'report': case.report, 'target': {'start_url': server.url(case.case_id, variant)}})
    write_json(folder / 'request.json', request.model_dump(mode='json'))
    started = perf_counter()
    meter = None
    result = {}
    cancelled = False
    row = {
        'trial_id': trial_id, 'case_id': case.case_id, 'variant': variant, 'repeat': repeat,
        'expected_status': expected_status(case, variant), 'actual_status': 'inconclusive',
        'artifact_dir': str(folder.resolve()), 'error': None, 'verification_attempted': False,
        'verification_seconds': None,
    }
    try:
        meter = MeteredClient(build_model_client(), folder / 'model_calls.json')
        async with asyncio.timeout(timeout):
            result = await execute_workflow(request, trial_id, folder, model_client=meter)
        row['actual_status'] = result['reproduction_status']
        row['error'] = result.get('error')
    except asyncio.CancelledError:
        cancelled = True
        row['error'] = {'type': 'CancelledError', 'message': '평가 시도가 취소됐습니다.'}
    except TimeoutError:
        row['error'] = {'type': 'TimeoutError', 'message': '평가 시도 제한 시간에 도달했습니다.'}
    except Exception as error:
        row['actual_status'] = 'environment_error'
        row['error'] = {'type': type(error).__name__, 'message': str(error)}
    row['elapsed_seconds'] = round(perf_counter() - started, 2)
    row['workflow_status'] = result.get('workflow_status')
    execution = result.get('execution', {})
    row['action_attempts'] = execution.get('action_attempts', 0)
    row['successful_actions'] = sum(item.get('success') is True for item in execution.get('history', []))
    generation = result.get('test_generation', {})
    row['test_generated'] = (generation.get('generation_status') == 'completed'
                             and (folder / 'regression' / 'case.json').is_file()
                             and (folder / 'regression' / 'test_regression.py').is_file())
    row['test_status'] = generation.get('test_status', 'not_generated')
    row['usage'] = meter.summary() if meter else {
        'model_calls': 0, 'model_errors': 0, 'input_tokens': 0, 'output_tokens': 0,
        'input_tokens_known_subtotal': 0, 'output_tokens_known_subtotal': 0,
        'input_tokens_known_calls': 0, 'output_tokens_known_calls': 0, 'models': [], 'backends': [],
    }
    if not cancelled and verify_tests and variant == 'buggy' and not case.needs_information and row['test_generated']:
        row['verification_attempted'] = True
        verification_started = perf_counter()
        try:
            runner = RecordedReplayRunner()
            await runner.run(folder, server.url(case.case_id, 'buggy'), purpose='original')
            replay = await runner.run(folder, server.url(case.case_id, 'fixed'), purpose='fix')
            row['test_status'] = replay['test_generation']['test_status']
        except asyncio.CancelledError:
            cancelled = True
            row['verification_error'] = {'type': 'CancelledError', 'message': '재실행 검증이 취소됐습니다.'}
        except Exception as error:
            row['verification_error'] = {'type': type(error).__name__, 'message': str(error)}
        row['verification_seconds'] = round(perf_counter() - verification_started, 2)
    write_json(folder / 'evaluation_row.json', row)
    return row, cancelled


def agent_fingerprint():
    """평가 때 사용한 앱 코드의 내용 식별값을 계산한다."""
    root = Path(__file__).resolve().parent.parent / 'app'
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*.py')):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
    return digest.hexdigest()


async def evaluate(args):
    """선택한 평가 자료를 모두 같은 실행 함수로 처리한다."""
    load_dotenv()
    root = Path('artifacts') / f'evaluation-{uuid4().hex}'
    root.mkdir(parents=True)
    cases = [case for case in CASES if args.case == 'all' or case.case_id == args.case]
    trials = [(case, variant, repeat) for repeat in range(1, args.repeats + 1)
              for case in cases for variant in (['buggy'] if case.needs_information else ['buggy', 'fixed'])]
    manifest = {'dataset_version': FIXTURE_VERSION, 'dataset_fingerprint': dataset_fingerprint(),
                'created_at': datetime.now(timezone.utc).isoformat(), 'status': 'running',
                'agent_code_fingerprint': agent_fingerprint(), 'python_version': platform.python_version(),
                'planned_trials': len(trials), 'selected_cases': [case.case_id for case in cases],
                'repeats': args.repeats, 'verify_tests': args.verify_tests, 'rows': []}
    write_json(root / 'evaluation.json', manifest)
    try:
        with FixtureServer() as server:
            for index, (case, variant, repeat) in enumerate(trials, 1):
                print(f'평가 {index}/{len(trials)}: {case.case_id} / {variant} / {repeat}회차', flush=True)
                row, cancelled = await run_trial(case, variant, repeat, server, root, args.timeout, args.verify_tests)
                manifest['rows'].append(row)
                write_json(root / 'evaluation.json', manifest)
                print('정답:', row['expected_status'], '/ 실제:', row['actual_status'], '/ 시간:', row['elapsed_seconds'], '초', flush=True)
                print('모델 호출:', row['usage']['model_calls'], '/ 테스트 생성:', row['test_generated'], flush=True)
                if row.get('error'):
                    print('오류:', row['error'].get('type'), row['error'].get('message'), flush=True)
                if cancelled:
                    manifest['status'] = 'interrupted'
                    break
            else:
                manifest['status'] = 'completed'
    finally:
        if manifest['status'] == 'running':
            manifest['status'] = 'interrupted'
        write_json(root / 'evaluation.json', manifest)
        summary = write_reports(root)
        print('정답 판정 일치:', format_rate(summary['exact_match']))
        print('평가 보고서:', (root / 'summary.md').resolve())
        print('이 결과는 선택한 평가 자료에 한정되며 다른 방식 대비 개선율은 아직 측정하지 않았습니다.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=['all'] + [case.case_id for case in CASES], default='all')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--verify-tests', action='store_true')
    args = parser.parse_args()
    if args.repeats < 1 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('반복 횟수와 제한 시간은 양수이고 유한해야 합니다.')
    try:
        asyncio.run(evaluate(args))
    except KeyboardInterrupt:
        print('평가를 중단했습니다. 저장한 시도와 보고서는 유지됩니다.')
