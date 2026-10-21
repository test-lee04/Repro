"""기존 회귀 테스트의 비동기 재실행 작업과 조회를 관리한다."""

import asyncio
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.testing.replay import RecordedReplayRunner, write_json
from app.testing.compiler import UnsupportedCompilation


class ReplayRequest(BaseModel):
    """재실행 주소, 환경 역할과 중복 접수 방지용 식별자를 받는다."""

    model_config = ConfigDict(extra='forbid')
    target_url: HttpUrl
    purpose: Literal['original', 'fix']
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class ReplayService:
    """원래 실행 폴더를 재사용하며 재실행 작업 상태만 별도로 관리한다."""

    def __init__(self, runs, runner=None):
        """소유 작업 서비스와 기존 재실행기를 연결한다."""
        self.runs = runs
        self.runner = runner if runner is not None else RecordedReplayRunner()
        self.records = {}
        self.tasks = {}
        self.semaphore = asyncio.Semaphore(1)
        self.closed = False
        for run_id in runs.records:
            folder = runs.folder(run_id) / 'api_replays'
            if folder.is_symlink():
                continue
            for path in folder.glob('*.json'):
                if path.is_symlink():
                    continue
                record = json.loads(path.read_text(encoding='utf-8'))
                if record['run_id'] != run_id or path.stem != record['replay_id']:
                    continue
                if record['status'] in {'queued', 'running', 'cancelling'}:
                    record.update(status='interrupted', error='서버 종료로 재실행이 중단됐습니다.')
                    write_json(path, record)
                self.records[(run_id, record['replay_id'])] = record

    def _save(self, record):
        """원래 작업 폴더 아래에 재실행 접수 상태를 저장한다."""
        folder = self.runs.folder(record['run_id']) / 'api_replays'
        folder.mkdir(exist_ok=True)
        record['updated_at'] = datetime.now(timezone.utc).isoformat()
        write_json(folder / f"{record['replay_id']}.json", record)

    def submit(self, run_id, request):
        """종료한 원래 작업의 동일한 테스트를 지정한 주소에서 재실행한다.

        Args:
            run_id: 생성한 테스트를 소유한 원래 작업 식별자.
            request: 실제 재실행 주소와 original 또는 fix 역할.

        Returns:
            원래 판정과 별도로 관리하는 재실행 작업 상태.

        Notes:
            같은 접수 식별자의 동일 요청은 기존 재실행 작업을 반환한다.
            한 원래 작업에는 한 번에 하나의 재실행만 허용한다.
        """
        state = self.runs.get(run_id)
        if self.closed:
            raise RuntimeError('서버가 종료 중입니다.')
        payload = request.model_dump(mode='json')
        for (owner, _), record in self.records.items():
            if owner == run_id and record['request_id'] == request.request_id:
                if record['request'] != payload:
                    raise RuntimeError('같은 접수 식별자로 다른 재실행을 요청할 수 없습니다.')
                return copy.deepcopy(record)
        if state['status'] != 'completed':
            raise RuntimeError('검사가 종료된 작업의 테스트만 재실행할 수 있습니다.')
        if any(owner == run_id and record['status'] in {'queued', 'running', 'cancelling'}
               for (owner, _), record in self.records.items()):
            raise RuntimeError('이 작업의 다른 재실행이 진행 중입니다.')
        result = self.runs.result(run_id)
        generation = result.get('test_generation', {})
        folder = self.runs.folder(run_id)
        if (generation.get('generation_status') != 'completed'
                or not (folder / 'regression' / 'case.json').is_file()
                or not (folder / 'regression' / 'test_regression.py').is_file()):
            raise ValueError('완료한 테스트 생성 기록과 실제 테스트 파일이 필요합니다.')
        replay_id = uuid4().hex
        record = {
            'run_id': run_id, 'replay_id': replay_id, 'request_id': request.request_id,
            'request': payload, 'status': 'queued', 'result': None, 'error': None,
            'created_at': datetime.now(timezone.utc).isoformat(),
        }
        key = (run_id, replay_id)
        self.records[key] = record
        self._save(record)
        task = asyncio.create_task(self._execute(record))
        self.tasks[key] = task
        task.add_done_callback(lambda completed: self._finished(key, completed))
        return copy.deepcopy(record)

    async def _execute(self, record):
        """기존 재실행기를 호출해 실제 테스트 상태를 갱신한다."""
        try:
            async with self.semaphore:
                record['status'] = 'running'
                self._save(record)
                request = record['request']
                record['result'] = await self.runner.run(
                    self.runs.folder(record['run_id']), request['target_url'],
                    purpose=request['purpose'],
                )
                record['status'] = 'completed'
        except asyncio.CancelledError:
            record['status'] = 'cancelled'
            raise
        except Exception as error:
            record.update(status='failed', error={'type': type(error).__name__, 'message': str(error)})
            if isinstance(error, (ValueError, OSError)):
                try:
                    self._invalidate_test(record['run_id'], error)
                except (ValueError, OSError) as save_error:
                    record['state_update_error'] = str(save_error)
        finally:
            self._save(record)

    def _invalidate_test(self, run_id, error):
        """생성 자료가 잘못됐으면 이전 검증 완료를 현재 상태로 유지하지 않는다."""
        result = self.runs.result(run_id)
        generation = result['test_generation']
        generation.update(
            test_status='unsupported' if isinstance(error, UnsupportedCompilation) else 'environment_error',
            validation_status='invalid_test', replay_status='not_run',
            reason='생성한 테스트 자료의 검사를 통과하지 못해 재실행을 시작하지 못했습니다.',
            error={'type': type(error).__name__, 'message': str(error)},
        )
        folder = self.runs.folder(run_id)
        write_json(folder / 'test_generation.json', generation)
        write_json(folder / 'workflow_result.json', result)

    def _finished(self, key, task):
        """시작 전에 취소한 작업도 종료 상태로 저장한다."""
        self.tasks.pop(key, None)
        record = self.records[key]
        if task.cancelled() and record['status'] == 'cancelling':
            record['status'] = 'cancelled'
            self._save(record)

    def get(self, run_id, replay_id):
        """원래 작업과 재실행 식별자가 모두 일치하는 기록만 반환한다."""
        self.runs.get(run_id)
        return copy.deepcopy(self.records[(run_id, replay_id)])

    def list(self, run_id):
        """해당 원래 작업의 재실행 접수 이력을 반환한다."""
        self.runs.get(run_id)
        return [copy.deepcopy(record) for (owner, _), record in self.records.items() if owner == run_id]

    def cancel(self, run_id, replay_id):
        """재실행만 취소하고 원래 제보 작업은 변경하지 않는다."""
        record = self.get(run_id, replay_id)
        key = (run_id, replay_id)
        if record['status'] in {'cancelled', 'cancelling'}:
            return record
        task = self.tasks.get(key)
        if task is None or task.done():
            raise RuntimeError('종료한 재실행은 취소할 수 없습니다.')
        self.records[key]['status'] = 'cancelling'
        self._save(self.records[key])
        task.cancel()
        return self.get(run_id, replay_id)

    async def close(self):
        """종료 시 재실행을 취소하고 기록 저장과 브라우저 정리를 기다린다."""
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
