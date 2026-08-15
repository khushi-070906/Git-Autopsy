from __future__ import annotations

import logging
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

from app.database import Analysis, CounterfactualJob, SessionLocal, get_session, init_db
from app.pipeline import run_analysis
from app.security import InvalidRepositoryURL, validate_github_url
from app.analysis import badge
from app.analysis.evidence_graph import evidence_for_node_json
from app.analysis import counterfactual
from app.analysis import regression_detection
from app.analysis.cloner import clone_repository, cleanup_workdir


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="AUTOPSY", description="Forensic analysis for software.", lifespan=lifespan)

app.include_router(badge.router)

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


@app.get("/api/analysis/{analysis_id}/graph/node/{node_id:path}")
def get_graph_node(analysis_id: str, node_id: str, db: Session = Depends(get_session)):
    """
    NEW. Powers the "click a node to see its evidence" interaction on the
    causal graph: returns one node's data plus its immediate incoming and
    outgoing edges.

    node_id uses the `:path` converter (not the default `str` converter)
    because node ids contain literal colons and slashes — e.g.
    "function:app/analysis/detect.py::detect_language" — which the default
    converter would otherwise treat as path-segment boundaries and 404 on.

    Operates on the persisted graph JSON via evidence_for_node_json rather
    than reconstructing an nx.MultiDiGraph, since only the serialized form
    survives between the background job that built the graph and this
    later request.
    """
    result = _completed_result(analysis_id, db)
    evidence = evidence_for_node_json(result["graph"], node_id)
    if "error" in evidence:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found in this analysis's graph.")
    return evidence


# --- Counterfactual replay -------------------------------------------------
#
# Job status/results are persisted in the CounterfactualJob table (see
# app/database.py) rather than an in-memory dict — survives process
# restarts and works correctly if this service ever runs more than one
# instance. Execution itself still runs in-process via BackgroundTasks;
# see counterfactual.py's module docstring and the Dockerfile's non-root
# user for the process-level hardening currently in place. True
# container/VM-level isolation (a separate execution service, or a
# provider like E2B/Modal/Daytona) is a larger infrastructure change,
# not something this table changes.

# Only frameworks counterfactual.py actually knows how to run. Anything
# else in test_frameworks (e.g. "unknown-*") is filtered out here so the
# endpoint can return a clear 400 instead of counterfactual.py raising
# UnsupportedTestFramework mid-background-task where the caller can't see it.
_SUPPORTED_FRAMEWORKS = {"pytest", "jest"}


class CounterfactualRequest(BaseModel):
    commit_sha: str


def _set_job_status(job_id: str, status: str, error: str | None = None, result: dict | None = None) -> None:
    db = SessionLocal()
    try:
        row = db.get(CounterfactualJob, job_id)
        if row is None:
            return
        row.status = status
        if error is not None:
            row.error = error
        if result is not None:
            row.set_result(result)
        db.commit()
    finally:
        db.close()


def _run_counterfactual_job(job_id: str, analysis_id: str, repo_url: str, commit_sha: str, framework: str) -> None:
    _set_job_status(job_id, "running")
    repo_dir = None
    try:
        repo_dir = clone_repository(repo_url)
        result = counterfactual.run_counterfactual(repo_dir, commit_sha, framework)

        _set_job_status(
            job_id,
            "completed",
            error=result.error,
            result={
                "commit_sha": result.commit_sha,
                "short_sha": result.short_sha,
                "framework": result.framework,
                "removes_failure": result.removes_failure,
                "baseline_failing_tests": result.baseline.failing_tests,
                "without_commit_failing_tests": result.without_commit.failing_tests,
                "baseline_timed_out": result.baseline.timed_out,
                "without_commit_timed_out": result.without_commit.timed_out,
            },
        )

        # Write the outcome back into the persisted analysis too, not just
        # this job row — the dashboard's SECONDARY FINDINGS section reads
        # Analysis.result()["regressions"], not the job table.
        db = SessionLocal()
        try:
            row = db.get(Analysis, analysis_id)
            if row is not None and row.status == "completed":
                stored = row.result()
                stored["regressions"] = regression_detection.apply_counterfactual_result(
                    stored.get("regressions", {}), result
                )
                row.set_result(stored)
                db.commit()
        finally:
            db.close()

    except Exception as exc:  # noqa: BLE001
        logging.getLogger("autopsy.main").exception(
            "Counterfactual job %s failed for analysis %s, commit %s", job_id, analysis_id, commit_sha
        )
        _set_job_status(job_id, "failed", error=str(exc)[:2000])
    finally:
        if repo_dir is not None:
            cleanup_workdir(repo_dir)


@app.post("/api/analysis/{analysis_id}/counterfactual")
@limiter.limit(ANALYZE_RATE_LIMIT)
def start_counterfactual(
    request: Request,
    analysis_id: str,
    req: CounterfactualRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_session),
):
    """
    Triggers an on-demand counterfactual replay for one suspect commit —
    "what if this commit had not been introduced?" — reusing the repo_url
    and test_frameworks already recorded for this analysis. Deliberately
    on-demand rather than run automatically for every suspect: each run
    clones the repo again and executes its test suite, which is expensive
    and carries real code-execution risk (see counterfactual.py's module
    docstring) — only worth paying for the specific commit a user is
    actually investigating.
    """
    result = _completed_result(analysis_id, db)
    row = _get_or_404(db, analysis_id)

    valid_shas = {c["sha"] for c in result["commits"]}
    if req.commit_sha not in valid_shas:
        raise HTTPException(status_code=400, detail="commit_sha not found in this analysis.")

    framework = next((f for f in result["test_frameworks"] if f in _SUPPORTED_FRAMEWORKS), None)
    if framework is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No counterfactual-supported test framework detected for this repo "
                f"(detected: {result['test_frameworks']}, supported: {sorted(_SUPPORTED_FRAMEWORKS)})."
            ),
        )

    job_id = uuid.uuid4().hex
    job_row = CounterfactualJob(
        id=job_id, analysis_id=analysis_id, commit_sha=req.commit_sha, status="queued"
    )
    db.add(job_row)
    db.commit()

    background_tasks.add_task(
        _run_counterfactual_job, job_id, analysis_id, row.repo_url, req.commit_sha, framework
    )
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/counterfactual/{job_id}")
def get_counterfactual_job(job_id: str, db: Session = Depends(get_session)):
    row = db.get(CounterfactualJob, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Counterfactual job not found.")
    return {"status": row.status, "error": row.error, "result": row.result()}


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


@app.get("/version")
def version():
    """
    Returns the deployed commit SHA. Railway sets RAILWAY_GIT_COMMIT_SHA
    automatically — this exists purely so a deploy can be verified with a
    single GET instead of comparing evidence-text phrasing or digging
    through build/runtime logs to infer whether a push actually went live.
    """
    return {"commit": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "unknown")}
