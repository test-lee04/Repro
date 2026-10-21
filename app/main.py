"""로컬 개발용 API 서버와 기존 데모 화면을 제공한다."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.run_service import RunService
from app.api.replay_service import ReplayService
from app.api.runs import router as runs_router

STATIC = Path(__file__).parent / 'static'


def create_app(run_service=None, replay_service=None):
    """작업 서비스가 서버 수명과 함께 시작하고 종료되도록 앱을 만든다.

    Args:
        run_service: API 검사에서 주입할 작업 서비스. 기본값은 실제 실행 서비스.
        replay_service: 검사에 주입할 재실행 서비스. 기본값은 실제 재실행 서비스.

    Returns:
        작업 접수 및 결과 조회 경로를 가진 FastAPI 앱.
    """
    @asynccontextmanager
    async def lifespan(application):
        application.state.run_service = run_service if run_service is not None else RunService()
        application.state.replay_service = replay_service if replay_service is not None else ReplayService(application.state.run_service)
        try:
            yield
        finally:
            try:
                await application.state.replay_service.close()
            finally:
                await application.state.run_service.close()

    application = FastAPI(title='ReproPilot', lifespan=lifespan)
    application.include_router(runs_router)
    application.mount('/static', StaticFiles(directory=STATIC), name='static')

    @application.get('/')
    async def index():
        """제보 입력, 추가 답변과 결과 조회 화면을 제공한다."""
        return FileResponse(STATIC / 'index.html')

    @application.get('/health')
    async def health():
        """서버 단계와 현재 연결한 기능을 반환한다."""
        return {'status': 'ok', 'stage': 'dashboard', 'agent_implemented': True,
                'answers_implemented': True, 'dashboard_implemented': True, 'replay_implemented': True}

    @application.get('/demo')
    async def demo():
        """기존 장바구니 검사 화면을 제공한다."""
        return FileResponse(STATIC / 'cart.html')

    return application


app = create_app()
