"""API contract only: no run is created until the service is implemented."""
from fastapi import APIRouter, HTTPException
from app.schemas import AnswersRequest, RunRequest

router = APIRouter(prefix="/api/runs", tags=["runs"])


def pending():
    raise HTTPException(status_code=501, detail="Skeleton: not implemented")


@router.post("")
def create_run(request: RunRequest):
    pending()


@router.get("/{run_id}")
def get_run(run_id: str):
    pending()


@router.post("/{run_id}/answers")
def answer_run(run_id: str, request: AnswersRequest):
    pending()


@router.post("/{run_id}/cancel")
def cancel_run(run_id: str):
    pending()


@router.get("/{run_id}/events")
def run_events(run_id: str):
    pending()


@router.get("/{run_id}/result")
def run_result(run_id: str):
    pending()


@router.get("/{run_id}/artifacts/{artifact_id}")
def get_artifact(run_id: str, artifact_id: str):
    pending()
