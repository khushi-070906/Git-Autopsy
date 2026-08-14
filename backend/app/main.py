from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager


from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app.database import Analysis, SessionLocal, get_session, init_db
from app.pipeline import run_analysis
from app.security import InvalidRepositoryURL, validate_github_url

@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/version")
def version():
    return {"commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "unknown")}

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="AUTOPSY", description="Forensic analysis for software.", lifespan=lifespan)

# ALLOWED_ORIGINS is a comma-separated list of exact origins, e.g.
# "https://autopsy.example.com,https://staging.autopsy.example.com".
# Left unset, we default to "*" for local development only — set it
# explicitly before deploying anywhere public.
_allowed_origins_env = os.environ.get("ALLOWED_ORIGINS", "").strip()
_allowed_origins = [o.strip() for o in _allowed_origins_env.split(",") if o.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

# Analysis jobs clone a repo and run static analysis — expensive enough that
# an unauthenticated client could exhaust CPU/disk by firing requests in a
# loop. Cap it per client IP; tune ANALYZE_RATE_LIMIT via env if needed.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
ANALYZE_RATE_LIMIT = os.environ.get("ANALYZE_RATE_LIMIT", "5/minute")


class AnalyzeRequest(BaseModel):
    repo_url: str


class AnalyzeResponse(BaseModel):
    id: str
    status: str


@app.post("/api/analyze", response_model=AnalyzeResponse)
@limiter.limit(ANALYZE_RATE_LIMIT)
def analyze(request: Request, req: AnalyzeRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_session)):
    try:
        validate_github_url(req.repo_url)
    except InvalidRepositoryURL as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    analysis_id = uuid.uuid4().hex
    row = Analysis(id=analysis_id, repo_url=req.repo_url, status="queued")
    db.add(row)
    db.commit()

    background_tasks.add_task(run_analysis, analysis_id, req.repo_url)
    return AnalyzeResponse(id=analysis_id, status="queued")


def _get_or_404(db: Session, analysis_id: str) -> Analysis:
    row = db.get(Analysis, analysis_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Analysis not found.")
    return row


@app.get("/api/analysis/{analysis_id}")
def get_analysis(analysis_id: str, db: Session = Depends(get_session)):
    row = _get_or_404(db, analysis_id)
    return {
        "id": row.id,
        "repo_url": row.repo_url,
        "status": row.status,
        "error": row.error,
        "result": row.result() if row.status == "completed" else None,
    }


def _completed_result(analysis_id: str, db: Session) -> dict:
    row = _get_or_404(db, analysis_id)
    if row.status != "completed":
        raise HTTPException(status_code=409, detail=f"Analysis status is '{row.status}', not completed.")
    return row.result()


@app.get("/api/analysis/{analysis_id}/commits")
def get_commits(analysis_id: str, db: Session = Depends(get_session)):
    return _completed_result(analysis_id, db)["commits"]


@app.get("/api/analysis/{analysis_id}/graph")
def get_graph(analysis_id: str, db: Session = Depends(get_session)):
    return _completed_result(analysis_id, db)["graph"]


@app.get("/api/analysis/{analysis_id}/regressions")
def get_regressions(analysis_id: str, db: Session = Depends(get_session)):
    return _completed_result(analysis_id, db)["regressions"]


@app.get("/api/analysis/{analysis_id}/dependencies")
def get_dependencies(analysis_id: str, db: Session = Depends(get_session)):
    result = _completed_result(analysis_id, db)
    return {
        "dependency_files": result["dependency_files"],
        "graph_dependency_nodes": [
            n for n in result["graph"]["nodes"] if n.get("kind") == "dependency"
        ],
    }


@app.get("/api/analysis/{analysis_id}/history")
def get_history(analysis_id: str, db: Session = Depends(get_session)):
    result = _completed_result(analysis_id, db)
    return {
        "commits": result["commits"],
        "functions": [n for n in result["graph"]["nodes"] if n.get("kind") == "function"],
    }


@app.get("/health")
def health_check():
    return {"status": "ok"}
