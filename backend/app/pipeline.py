"""
Orchestrates Phases 1-8 into a single background job. This is the only
place that sequences the analysis engine end to end; each phase module
stays independently testable.
"""
from __future__ import annotations

import logging

from app.analysis import (
    cloner,
    detect,
    evidence_graph,
    git_history,
    regression_detection,
    static_python,
    why_analysis,
)
from app.analysis.ai_layer import explain_suspect
from app.database import Analysis, SessionLocal

logger = logging.getLogger("autopsy.pipeline")

STATUS_SEQUENCE = ["queued", "cloning", "indexing", "building_graph", "analyzing", "completed"]


def _update_status(analysis_id: str, status: str, error: str | None = None) -> None:
    db = SessionLocal()
    try:
        row = db.get(Analysis, analysis_id)
        if row is None:
            return
        row.status = status
        if error:
            row.error = error
        db.commit()
    finally:
        db.close()


def run_analysis(analysis_id: str, repo_url: str) -> None:
    """
    Runs the full pipeline for one job. Called from a background task /
    thread so the HTTP request that triggered it is never blocked.
    """
    repo_dir = None
    try:
        _update_status(analysis_id, "cloning")
        repo_dir = cloner.clone_repository(repo_url)

        _update_status(analysis_id, "indexing")
        commits = git_history.extract_history(repo_dir)
        language_info = detect.detect_language(repo_dir)
        dep_files = detect.detect_dependency_files(repo_dir)
        test_frameworks = detect.detect_test_framework(repo_dir)
        file_analyses = (
            static_python.analyze_repository_python_files(repo_dir)
            if language_info["dominant_language"] == "Python"
            else []
        )

        # has_test_framework: True only for a real, named framework
        # (pytest, jest, ...). has_weak_test_signal: True when all we
        # found was a bare test directory with no framework markers —
        # weaker evidence than a real framework, but more than nothing,
        # so it gets a looser confidence cap downstream than "no signal
        # at all".
        real_frameworks = [f for f in test_frameworks if not f.startswith("unknown")]
        has_test_framework = bool(real_frameworks)
        has_weak_test_signal = bool(test_frameworks) and not has_test_framework

        _update_status(analysis_id, "building_graph")
        g = evidence_graph.build_evidence_graph(repo_dir, commits, file_analyses)

        _update_status(analysis_id, "analyzing")
        suspects = why_analysis.rank_suspects(
            g,
            has_test_framework=has_test_framework,
            has_weak_test_signal=has_weak_test_signal,
        )
        regressions = regression_detection.detect_regressions(
            g,
            has_test_execution_data=False,
            has_test_framework=has_test_framework,
            has_weak_test_signal=has_weak_test_signal,
        )
        health = regression_detection.compute_repository_health(g, suspects)

        top_root_cause = None
        if suspects:
            top = suspects[0]
            top_root_cause = {
                "commit_sha": top.commit_sha,
                "short_sha": top.short_sha,
                "message": top.message,
                "confidence": top.confidence,
                "summary": top.likely_cause_summary,
                "affected_files": top.affected_files,
                "affected_functions": top.affected_functions,
                "evidence": [{"kind": e.kind, "text": e.text} for e in top.evidence],
                "ai_explanation": explain_suspect(top),
            }

        result = {
            "repo_url": repo_url,
            "language": language_info,
            "dependency_files": dep_files,
            "test_frameworks": test_frameworks,
            "health": health,
            "top_root_cause": top_root_cause,
            "suspects": [
                {
                    "commit_sha": s.commit_sha,
                    "short_sha": s.short_sha,
                    "message": s.message,
                    "author": s.author,
                    "date": s.date,
                    "confidence": s.confidence,
                    "summary": s.likely_cause_summary,
                    "affected_files": s.affected_files,
                    "affected_functions": s.affected_functions,
                    "evidence": [{"kind": e.kind, "text": e.text} for e in s.evidence],
                }
                for s in suspects
            ],
            "regressions": regressions,
            "commit_count": len(commits),
            "commits": [
                {
                    "sha": c.sha, "short_sha": c.short_sha, "author": c.author,
                    "date": c.date, "message": c.message.splitlines()[0][:200] if c.message else "",
                    "files_changed": [fc.path for fc in c.files_changed],
                }
                for c in commits
            ],
            "graph": evidence_graph.graph_to_json(g),
        }

        db = SessionLocal()
        try:
            row = db.get(Analysis, analysis_id)
            row.set_result(result)
            row.status = "completed"
            db.commit()
        finally:
            db.close()

    except Exception as exc:  # noqa: BLE001
        logger.exception("Analysis %s failed", analysis_id)
        _update_status(analysis_id, "failed", error=str(exc)[:2000])
    finally:
        if repo_dir is not None:
            cloner.cleanup_workdir(repo_dir)
