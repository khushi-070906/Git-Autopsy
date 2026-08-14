"""
Phase 7 — Regression detection.

Without executed test history, AUTOPSY previously could only ever report
"insufficient_data" — static heuristics flagging PLAUSIBLE triggers, never
a confirmed cause. This module now accepts an optional `confirmed_regression`
(produced by ci_status.find_confirmed_regression from real GitHub Actions /
Checks API data) — when one is found, the response reports a genuine
last-passing -> first-failing transition instead of only a static guess.
This only applies to repos that actually have CI configured and that
AUTOPSY was able to reach GitHub's API for; everything else still falls
back to the original heuristic-only behavior unchanged.
"""
from __future__ import annotations

from dataclasses import asdict

import networkx as nx

from app.analysis.why_analysis import Suspect, rank_suspects


def _suspect_to_dict(s: Suspect) -> dict:
    d = asdict(s)
    return d


def detect_regressions(
    g: nx.MultiDiGraph,
    has_test_execution_data: bool = False,
    has_test_framework: bool = True,
    has_weak_test_signal: bool = False,
    confirmed_regression: dict | None = None,
) -> dict:
    """
    `has_test_execution_data`: whether we ran tests ourselves in a sandbox
    (still always False — AUTOPSY has no sandboxed execution engine).

    `confirmed_regression`: optional dict from
    ci_status.find_confirmed_regression, containing a real last-passing ->
    first-failing transition sourced from GitHub's Checks API. When
    present, this is genuine evidence — not a static guess — so it's
    surfaced distinctly with status "confirmed" rather than folded into
    the "PLAUSIBLE... not confirmed" heuristic list.
    """
    heuristic_suspects = rank_suspects(
        g, top_n=10, min_confidence=0.3,
        has_test_framework=has_test_framework,
        has_weak_test_signal=has_weak_test_signal,
    )

    if confirmed_regression is not None:
        return {
            "status": "confirmed",
            "message": (
                f"Confirmed via GitHub CI history: commit {confirmed_regression['short_sha']} "
                f"is the first commit where CI failed after commit "
                f"{confirmed_regression['last_passing_short_sha']} last passed."
            ),
            "confirmed_regression": confirmed_regression,
            "suspicious_changes": [_suspect_to_dict(s) for s in heuristic_suspects],
            "note": (
                "The confirmed_regression field above is sourced from real GitHub Actions / "
                "Checks API results — a genuine pass/fail transition, not a static-heuristic "
                "guess. The suspicious_changes list below is still the same PLAUSIBLE-trigger "
                "heuristic ranking as always, included for context."
            ),
        }

    if not has_test_execution_data:
        return {
            "status": "insufficient_data",
            "message": "Insufficient historical test evidence.",
            "suspicious_changes": [_suspect_to_dict(s) for s in heuristic_suspects],
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
