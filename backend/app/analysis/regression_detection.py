"""
Phase 7 — Regression detection.

V1 has no sandboxed test-execution engine (spec requires that to be a
clearly separated, optional feature — not implemented in this MVP). Without
executed test history, AUTOPSY cannot claim to know which tests failed when.
Per spec, it must say so explicitly instead of fabricating regression
evidence.

What it CAN do deterministically from git history alone:
  - flag commits that touch dependency manifests (potential regression
    triggers)
  - flag commits with large, high-risk diffs
  - surface these as "suspicious changes", clearly labeled as unconfirmed
    without executed test evidence
"""
from __future__ import annotations

from dataclasses import asdict

import networkx as nx

from app.analysis.why_analysis import Suspect, rank_suspects


def _suspect_to_dict(s: Suspect) -> dict:
    d = asdict(s)
    return d


def detect_regressions(g: nx.MultiDiGraph, has_test_execution_data: bool = False) -> dict:
    if not has_test_execution_data:
        return {
            "status": "insufficient_data",
            "message": "Insufficient historical test evidence.",
            "suspicious_changes": [
                _suspect_to_dict(s) for s in rank_suspects(g, top_n=10, min_confidence=0.3)
            ],
            "note": (
                "No executed test-pass/fail history is available for this repository. "
                "The items below are commits flagged by static heuristics (dependency "
                "changes, large diffs, risk-signaling commit messages) as PLAUSIBLE "
                "regression triggers — they are not confirmed regressions."
            ),
        }
    # Reserved for Phase 7+ optional isolated test-execution feature.
    raise NotImplementedError("Test-execution-based regression detection is not enabled in V1.")


def compute_repository_health(g: nx.MultiDiGraph, suspects: list[Suspect]) -> dict:
    """
    Produce the dashboard's headline numbers. Deterministic, formula-based —
    not an LLM guess.
    """
    num_commits = sum(1 for _, d in g.nodes(data=True) if d.get("kind") == "commit")
    num_files = sum(1 for _, d in g.nodes(data=True) if d.get("kind") == "file")
    num_deps = sum(1 for _, d in g.nodes(data=True) if d.get("kind") == "dependency")

    high_conf_suspects = [s for s in suspects if s.confidence >= 0.6]
    risk_penalty = min(60, len(high_conf_suspects) * 12 + len(suspects) * 3)
    score = max(0, 100 - risk_penalty)

    if score >= 80:
        risk_level = "LOW"
    elif score >= 55:
        risk_level = "MEDIUM"
    else:
        risk_level = "HIGH"

    return {
        "repository_health_score": score,
        "risk_level": risk_level,
        "likely_regressions": len(high_conf_suspects),
        "dependency_risks": num_deps,
        "suspicious_changes": len(suspects),
        "commits_analyzed": num_commits,
        "files_analyzed": num_files,
    }
