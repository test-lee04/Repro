from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from app.api.runs import router as runs_router

app = FastAPI(title="ReproPilot demo")
app.include_router(runs_router)
STATIC = Path(__file__).parent / "static"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "stage": "skeleton", "agent_implemented": False}


@app.get("/demo")
def demo():
    return FileResponse(STATIC / "cart.html")
