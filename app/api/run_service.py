"""제보 작업의 접수, 백그라운드 실행, 결과와 파일 조회를 관리한다."""

import asyncio
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.testing.replay import write_json


async def execute_workflow(request, run_id, folder):
    """기존 재현 흐름을 실행하고 브라우저 환경을 정리한다.

    Args:
        request: URL과 제보가 검증된 실행 요청.
        run_id: API가 생성한 작업 식별자.
        folder: 작업별 결과 저장 폴더.

    Returns:
        기존 실행 흐름의 최종 결과.
    """
    from dotenv import load_dotenv
    from app.agent.action_planner import ActionPlanner
    from app.agent.checkpoint_reviewer import CheckpointReviewer
    from app.agent.model_client import build_model_client
    from app.agent.report_parser import ReportParser
    from app.agent.reproduction_assessor import ReproductionAssessor
    from app.agent.reproduction_loop import ReproductionLoop
    from app.agent.reproduction_workflow import ReproductionWorkflow
    from app.agent.target_binder import TargetBinder
    from app.agent.verification_planner import VerificationPlanner
    from app.browser.session import PlaywrightSessions
    from app.browser.tools import PlaywrightTools
    from app.browser.verification import VerificationBrowser

    load_dotenv()
    client = build_model_client()
    async with PlaywrightSessions(targets={}) as sessions:
        session_id = await sessions.open(run_id=run_id, start_url=str(request.target.start_url))
        workflow = ReproductionWorkflow(
            ReportParser(client), VerificationPlanner(client),
            ReproductionLoop(ActionPlanner(client), PlaywrightTools(sessions, folder)),
            CheckpointReviewer(client), TargetBinder(client, diagnostic_dir=folder),
            VerificationBrowser(sessions, folder),
            assessor=ReproductionAssessor(client),
        )
        return await workflow.run(
            report=request.report, public_spec=request.target.public_spec or '',
            session_id=session_id, artifact_dir=folder,
        )


class RunService:
    """단일 서버 프로세스에서 작업을 관리하고 결과를 디스크에 보존한다."""

    def __init__(self, root=Path('artifacts'), worker=None):
        """저장 위치와 검사 대역을 주입하고 이전 중단 작업을 표시한다."""
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.worker = worker if worker is not None else execute_workflow
        self.tasks = {}
        self.records = {}
        self.semaphore = asyncio.Semaphore(1)
        self.closed = False
        for path in self.root.glob('api-run-*/api_state.json'):
            if path.is_symlink() or path.parent.is_symlink():
                continue
            record = json.loads(path.read_text(encoding='utf-8'))
            run_id = record['run_id']
            if path.parent.name != f'api-run-{run_id}':
                continue
            if record['status'] in {'queued', 'running', 'cancelling'}:
                record.update(status='interrupted', error='서버가 종료돼 실행이 중단됐습니다.')
                write_json(path, record)
            self.records[run_id] = record

    def folder(self, run_id):
        """서버가 발급한 식별자에 해당하는 폴더만 반환한다."""
        if run_id not in self.records:
            raise KeyError(run_id)
        return self.root / f'api-run-{run_id}'

    def save(self, run_id):
        """작업 상태와 갱신 시각을 함께 저장한다."""
        record = self.records[run_id]
        record['updated_at'] = datetime.now(timezone.utc).isoformat()
        write_json(self.folder(run_id) / 'api_state.json', record)

    def submit(self, request):
        """작업을 접수한 뒤 실행 완료를 기다리지 않고 식별자를 반환한다."""
        if self.closed:
            raise RuntimeError('서버가 종료 중입니다.')
        if request.target is None or request.mode.value != 'agent':
            raise ValueError('현재 API는 target URL과 agent 모드를 지원합니다.')
        run_id = uuid4().hex
        self.records[run_id] = {
            'run_id': run_id, 'status': 'queued', 'error': None,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'result_url': f'/api/runs/{run_id}/result',
            'artifacts_url': f'/api/runs/{run_id}/artifacts',
        }
        folder = self.folder(run_id)
        folder.mkdir()
        write_json(folder / 'request.json', request.model_dump(mode='json'))
        self.save(run_id)
        task = asyncio.create_task(self._execute(run_id, request))
        self.tasks[run_id] = task
        task.add_done_callback(lambda completed: self._finished(run_id, completed))
        return self.get(run_id)

    def _finished(self, run_id, task):
        """실행 시작 전의 취소도 종료 상태로 저장한다."""
        self.tasks.pop(run_id, None)
        if task.cancelled() and self.records[run_id]['status'] == 'cancelling':
            self.records[run_id]['status'] = 'cancelled'
            self.save(run_id)

    async def _execute(self, run_id, request):
        """동시 실행을 한 건으로 제한하고 기존 실행 처리를 호출한다."""
        record = self.records[run_id]
        try:
            async with self.semaphore:
                record['status'] = 'running'
                self.save(run_id)
                result = await self.worker(request, run_id, self.folder(run_id))
                write_json(self.folder(run_id) / 'workflow_result.json', result)
                workflow_status = result.get('workflow_status')
                record['status'] = (
                    'needs_information' if workflow_status == 'needs_information'
                    else 'failed' if workflow_status in {'error', 'tool_error'}
                    else 'completed'
                )
                record['reproduction_status'] = result.get('reproduction_status')
                record['error'] = result.get('error')
        except asyncio.CancelledError:
            record['status'] = 'cancelled'
            raise
        except Exception as error:
            record.update(status='failed', error={'type': type(error).__name__, 'message': str(error)})
        finally:
            self.save(run_id)

    def get(self, run_id):
        """내부 상태를 바꿀 수 없도록 복사본을 반환한다."""
        self.folder(run_id)
        return copy.deepcopy(self.records[run_id])

    def result(self, run_id):
        """저장한 결과를 반환하고 실행 중이면 재조회를 요청한다."""
        folder = self.folder(run_id)
        if self.records[run_id]['status'] in {'queued', 'running', 'cancelling'}:
            raise RuntimeError('실행 중입니다. 상태를 확인하고 다시 조회하세요.')
        path = folder / 'workflow_result.json'
        if not path.is_file():
            raise RuntimeError('실행 결과가 없습니다. 작업 상태와 오류를 확인하세요.')
        return json.loads(path.read_text(encoding='utf-8'))

    def cancel(self, run_id):
        """실행 중이거나 대기 중인 작업에 취소를 요청한다."""
        record = self.get(run_id)
        if record['status'] in {'cancelled', 'cancelling'}:
            return record
        task = self.tasks.get(run_id)
        if task is None or task.done():
            raise RuntimeError('종료한 작업은 취소할 수 없습니다.')
        self.records[run_id]['status'] = 'cancelling'
        self.save(run_id)
        task.cancel()
        return self.get(run_id)

    def artifacts(self, run_id):
        """종료 후 결과 파일에 다운로드용 식별자를 부여한다."""
        self.result(run_id)
        folder = self.folder(run_id).resolve()
        files = []
        for path in sorted(folder.rglob('*')):
            if path.is_symlink() or not path.is_file() or path.suffix not in {'.json', '.png', '.py', '.txt', '.md'}:
                continue
            if not path.resolve().is_relative_to(folder):
                continue
            relative = path.relative_to(folder).as_posix()
            if relative in {'request.json', 'api_state.json'}:
                continue
            artifact_id = hashlib.sha256(relative.encode()).hexdigest()
            files.append({'artifact_id': artifact_id, 'name': relative,
                          'download_url': f'/api/runs/{run_id}/artifacts/{artifact_id}'})
        return files

    def artifact(self, run_id, artifact_id):
        """목록에 등록한 파일만 가져온다."""
        for item in self.artifacts(run_id):
            if item['artifact_id'] == artifact_id:
                return self.folder(run_id) / item['name']
        raise KeyError(artifact_id)

    async def close(self):
        """서버 종료 시 작업을 취소하고 정리를 기다린다."""
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
