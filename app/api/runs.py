"""제보 작업의 접수, 상태, 결과와 파일 조회 API를 제공한다."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from app.schemas import AnswersRequest, RunRequest
from app.api.replay_service import ReplayRequest

router = APIRouter(prefix='/api/runs', tags=['runs'])


def service(request: Request):
    """현재 서버가 관리하는 작업 서비스를 가져온다."""
    return request.app.state.run_service


def call(function, *args):
    """서비스 예외를 API 상태 코드로 변환한다."""
    try:
        return function(*args)
    except KeyError as error:
        raise HTTPException(404, '작업 또는 파일을 찾을 수 없습니다.') from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error


@router.post('', status_code=202)
async def create_run(payload: RunRequest, request: Request):
    """제보를 접수하고 작업 식별자를 즉시 반환한다."""
    return call(service(request).submit, payload)


@router.get('/{run_id}')
async def get_run(run_id: str, request: Request):
    """작업 상태와 결과 조회 주소를 반환한다."""
    return call(service(request).get, run_id)


@router.get('/{run_id}/result')
async def run_result(run_id: str, request: Request):
    """완료한 실행의 판정, 질문, 비교 기록과 테스트 상태를 반환한다."""
    return call(service(request).result, run_id)


@router.post('/{run_id}/cancel', status_code=202)
async def cancel_run(run_id: str, request: Request):
    """실행 취소를 요청한다."""
    return call(service(request).cancel, run_id)


@router.get('/{run_id}/artifacts')
async def list_artifacts(run_id: str, request: Request):
    """결과 및 증거 파일 목록과 다운로드 주소를 반환한다."""
    return {'artifacts': call(service(request).artifacts, run_id)}


@router.get('/{run_id}/artifacts/{artifact_id}')
async def get_artifact(run_id: str, artifact_id: str, request: Request):
    """파일 경로 대신 서버가 발급한 식별자로 파일을 다운로드한다."""
    path = call(service(request).artifact, run_id, artifact_id)
    return FileResponse(path, filename=path.name, media_type='application/octet-stream')


@router.post('/{run_id}/answers', status_code=202)
async def answer_run(run_id: str, payload: AnswersRequest, request: Request):
    """실제 질문에 답변하고 연결된 새 실행을 접수한다."""
    return call(service(request).answer, run_id, payload.answers)


@router.get('/{run_id}/events')
async def run_events(run_id: str, request: Request):
    """현재 단계에서는 상태 조회 API로 진행 상태를 확인한다."""
    call(service(request).get, run_id)
    raise HTTPException(501, '현재는 작업 상태 조회 API를 주기적으로 호출하세요.')


@router.post('/{run_id}/replays', status_code=202)
async def create_replay(run_id: str, payload: ReplayRequest, request: Request):
    """생성한 동일한 테스트의 재실행을 접수한다."""
    return call(request.app.state.replay_service.submit, run_id, payload)


@router.get('/{run_id}/replays')
async def list_replays(run_id: str, request: Request):
    """재실행 작업 이력을 반환한다."""
    return {'replays': call(request.app.state.replay_service.list, run_id)}


@router.get('/{run_id}/replays/{replay_id}')
async def get_replay(run_id: str, replay_id: str, request: Request):
    """재실행 상태와 결과를 반환한다."""
    return call(request.app.state.replay_service.get, run_id, replay_id)


@router.post('/{run_id}/replays/{replay_id}/cancel', status_code=202)
async def cancel_replay(run_id: str, replay_id: str, request: Request):
    """재실행만 취소한다."""
    return call(request.app.state.replay_service.cancel, run_id, replay_id)
