"""제보 작업의 접수, 백그라운드 실행, 결과와 파일 조회를 관리한다."""

import asyncio
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.testing.replay import write_json


async def execute_workflow(request, run_id, folder, *, answers=None):
    """기존 재현 흐름을 실행하고 브라우저 환경을 정리한다.

    Args:
        request: URL과 제보가 검증된 실행 요청.
        run_id: API가 생성한 작업 식별자.
        folder: 작업별 결과 저장 폴더.
        answers: 실제 질문과 사용자 답변의 누적 기록.

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
            session_id=session_id, artifact_dir=folder, answers=answers,
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
        """새 제보를 접수하고 작업 식별자를 즉시 반환한다."""
        return self._submit(request, history=[], parent=None)

    def _submit(self, request, *, history, parent):
        """원래 요청과 누적 답변으로 독립된 실행 폴더를 만든다."""
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
            'parent_run_id': parent['run_id'] if parent else None,
            'root_run_id': parent.get('root_run_id', parent['run_id']) if parent else run_id,
            'clarification_round': parent.get('clarification_round', 0) + 1 if parent else 0,
            'questions': [],
        }
        folder = self.folder(run_id)
        folder.mkdir()
        write_json(folder / 'request.json', request.model_dump(mode='json'))
        write_json(folder / 'clarification_history.json', {'answers': history})
        self.save(run_id)
        task = asyncio.create_task(self._execute(run_id, request, history))
        self.tasks[run_id] = task
        task.add_done_callback(lambda completed: self._finished(run_id, completed))
        return self.get(run_id)

    def _finished(self, run_id, task):
        """실행 시작 전의 취소도 종료 상태로 저장한다."""
        self.tasks.pop(run_id, None)
        if task.cancelled() and self.records[run_id]['status'] == 'cancelling':
            self.records[run_id]['status'] = 'cancelled'
            self.save(run_id)

    async def _execute(self, run_id, request, history):
        """동시 실행을 한 건으로 제한하고 기존 실행 처리를 호출한다."""
        record = self.records[run_id]
        try:
            async with self.semaphore:
                record['status'] = 'running'
                self.save(run_id)
                if history:
                    from app.agent.report_schema import ClarificationAnswer
                    answers = [ClarificationAnswer(question=item['question'], answer=item['answer'])
                               for item in history]
                    result = await self.worker(request, run_id, self.folder(run_id), answers=answers)
                else:
                    result = await self.worker(request, run_id, self.folder(run_id))
                if result.get('workflow_status') == 'needs_information':
                    record['questions'] = self._question_entries(run_id, result)
                    result['clarification_questions'] = copy.deepcopy(record['questions'])
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

    @staticmethod
    def _question_entries(run_id, result):
        """원래 질문 문구에 작업별 고정 식별자를 부여한다."""
        questions = result.get('questions')
        if not isinstance(questions, list) or not questions:
            raise ValueError('정보 부족 상태에는 실제 확인 질문이 필요합니다.')
        entries = []
        for index, question in enumerate(questions, 1):
            if not isinstance(question, str) or not question.strip():
                raise ValueError('확인 질문은 비어 있지 않은 문자열이어야 합니다.')
            entries.append({'question_id': f'{run_id}-q{index}', 'question': question})
        return entries

    def answer(self, run_id, submitted):
        """서버가 제시한 질문의 답변을 받아 연결된 새 실행을 만든다.

        Args:
            run_id: 추가 질문이 필요한 이전 작업 식별자.
            submitted: 질문 식별자를 키로 사용하는 답변 사전.

        Returns:
            원래 제보와 누적 답변을 사용하는 새 작업의 상태.

        Notes:
            이전 결과와 증거를 보존하며 브라우저를 새로 연다.
            이미 처리한 동일 답변은 기존 후속 작업을 반환한다.
        """
        parent = self.get(run_id)
        if parent['status'] != 'needs_information':
            raise RuntimeError('추가 질문이 필요한 작업에만 답변할 수 있습니다.')
        questions = parent.get('questions') or self._question_entries(run_id, self.result(run_id))
        expected_ids = {item['question_id'] for item in questions}
        if not isinstance(submitted, dict) or set(submitted) != expected_ids:
            raise ValueError('현재 작업의 모든 질문 식별자에 답변을 하나씩 제출하세요.')
        if any(not isinstance(value, str) or not value.strip() or len(value) > 10000
               for value in submitted.values()):
            raise ValueError('각 답변은 비어 있지 않은 10000자 이하의 문자열이어야 합니다.')
        normalized = {key: value.strip() for key, value in submitted.items()}
        previous = parent.get('submitted_answers')
        if parent.get('child_run_id'):
            if previous == normalized:
                return self.get(parent['child_run_id'])
            raise RuntimeError('이미 답변한 작업입니다. 연결된 새 작업의 질문을 확인하세요.')
        if parent.get('clarification_round', 0) >= 5:
            raise RuntimeError('추가 답변은 최대 5회까지 연결합니다. 새 제보로 내용을 정리하세요.')
        from app.schemas import RunRequest
        folder = self.folder(run_id)
        request = RunRequest.model_validate_json((folder / 'request.json').read_text(encoding='utf-8'))
        history_path = folder / 'clarification_history.json'
        history = json.loads(history_path.read_text(encoding='utf-8'))['answers'] if history_path.exists() else []
        history.extend({'question_id': item['question_id'], 'question': item['question'],
                        'answer': normalized[item['question_id']], 'source_run_id': run_id}
                       for item in questions)
        child = self._submit(request, history=history, parent=parent)
        self.records[run_id].update(child_run_id=child['run_id'], submitted_answers=normalized,
                                    questions=questions)
        self.save(run_id)
        return child

    def get(self, run_id):
        """내부 상태를 바꿀 수 없도록 복사본을 반환한다."""
        self.folder(run_id)
        record = self.records[run_id]
        if record['status'] == 'needs_information' and not record.get('questions'):
            record['questions'] = self._question_entries(run_id, self.result(run_id))
            self.save(run_id)
        return copy.deepcopy(record)

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
