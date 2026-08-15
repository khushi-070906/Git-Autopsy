"""
Phase 7 — Regression detection.

Without executed test history, AUTOPSY previously could only ever report
"insufficient_data" — static heuristics flagging PLAUSIBLE triggers, never
a confirmed cause. This module accepts two independent, optional sources of
real (non-heuristic) confirmation:

  1. `confirmed_regression` — produced by ci_status.find_confirmed_regression
     from real GitHub Actions / Checks API data (a genuine last-passing ->
     first-failing transition observed in the repo's own CI history).
  2. `counterfactual_result` — produced by counterfactual.run_counterfactual,
     from an isolated replay: the test suite run with a suspect commit
     reverted, showing that removing it eliminates a specific failing test.

Either one, when present, upgrades the response from a static guess to a
"confirmed" status. They are independent and can both be present (CI status
is checked automatically for every analysis; counterfactual is triggered
on-demand per commit from the graph UI) — CI confirmation is preferred when
both exist, since it reflects the repo's own real-world test run rather
than AUTOPSY's best-effort reconstruction of one.

Repos with neither still fall back to the original heuristic-only behavior,
unchanged.
"""
from __future__ import annotations

from dataclasses import asdict

import networkx as nx

from app.analysis.why_analysis import Suspect, rank_suspects


def _suspect_to_dict(s: Suspect) -> dict:
    d = asdict(s)
    return d


def _counterfactual_to_dict(result) -> dict:
    """
    Shapes a counterfactual.CounterfactualResult into the same
    confirmed_regression dict shape ci_status.find_confirmed_regression
    already produces, so the frontend's confirmed-regression rendering
    doesn't need to branch on which source produced it.
    """
    return {
        "short_sha": result.short_sha,
        "commit_sha": result.commit_sha,
        "source": "counterfactual_replay",
        "framework": result.framework,
        "failing_tests_with_commit": result.baseline.failing_tests,
        "failing_tests_without_commit": result.without_commit.failing_tests,
    }


def apply_counterfactual_result(existing_regressions: dict, counterfactual_result) -> dict:
    """
    NEW. Layers a completed on-demand counterfactual run onto an
    *already-computed* regressions dict (the one sitting in a stored
    Analysis row's result()), without re-running rank_suspects() or
    needing the live evidence graph — the caller (main.py's
    _run_counterfactual_job) only has the persisted analysis result on
    hand, not the nx.MultiDiGraph that built it.

    This is what makes a completed replay actually show up in the
    dashboard's SECONDARY FINDINGS section instead of staying stuck at
    "insufficient_data" forever after a successful verification — without
    this, the counterfactual result only ever lived in the separate
    per-job dict the graph panel polls, and the main analysis record
    never learned about it.

    `existing_regressions` should be `analysis_result["regressions"]` as
    already stored (i.e. whatever detect_regressions() previously
    returned — most commonly the "insufficient_data" heuristic-only
    shape, since that's the default before any confirmation exists).
    Does not overwrite a CI-confirmed regression if one is already
    present — CI confirmation reflects the repo's own real-world test
    run and takes precedence over AUTOPSY's own reconstructed replay.
    """
    if existing_regressions.get("status") == "confirmed" and "confirmed_regression" in existing_regressions:
        if existing_regressions["confirmed_regression"].get("source") != "counterfactual_replay":
            # Already CI-confirmed — leave it alone.
            return existing_regressions

    if counterfactual_result.error is not None:
        # Replay itself failed (timeout, conflict, missing test runner) —
        # don't touch the existing regressions dict; nothing new was learned.
        return existing_regressions

    if counterfactual_result.removes_failure:
        updated = dict(existing_regressions)
        updated["status"] = "confirmed"
        updated["message"] = (
            f"Confirmed via counterfactual replay: re-running the test suite with "
            f"commit {counterfactual_result.short_sha} reverted eliminates a failing "
            f"test that is present with the commit applied."
        )
        updated["confirmed_regression"] = _counterfactual_to_dict(counterfactual_result)
        updated["note"] = (
            "The confirmed_regression field above is sourced from an isolated replay "
            "AUTOPSY ran itself (test suite executed with and without the commit) — "
            "a genuine result, not a static-heuristic guess."
        )
        return updated

    # Ran successfully but didn't confirm this commit — still worth
    # surfacing as a ruled-out candidate rather than silently discarding
    # a real result.
    updated = dict(existing_regressions)
    updated["ruled_out"] = _counterfactual_to_dict(counterfactual_result)
    return updated


def detect_regressions(
    g: nx.MultiDiGraph,
    has_test_execution_data: bool = False,
    has_test_framework: bool = True,
    has_weak_test_signal: bool = False,
    confirmed_regression: dict | None = None,
    counterfactual_result=None,
) -> dict:
    """
    `has_test_execution_data`: whether we ran tests ourselves in a sandbox
    for the commit currently being evaluated. True exactly when
    `counterfactual_result` is provided and it successfully ran (no error,
    no timeout on either side) — see pipeline.py / the counterfactual
    endpoint for where this is set.

    `confirmed_regression`: optional dict from
    ci_status.find_confirmed_regression — see module docstring.

    `counterfactual_result`: optional counterfactual.CounterfactualResult
    from an on-demand replay for one specific suspect commit — see module
    docstring. Only ever covers one commit per call (whichever the user
    asked to verify), unlike confirmed_regression which is repo-wide.
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

    if counterfactual_result is not None and counterfactual_result.error is None:
        if counterfactual_result.removes_failure:
            return {
                "status": "confirmed",
                "message": (
                    f"Confirmed via counterfactual replay: re-running the test suite with "
                    f"commit {counterfactual_result.short_sha} reverted eliminates a failing "
                    f"test that is present with the commit applied."
                ),
                "confirmed_regression": _counterfactual_to_dict(counterfactual_result),
                "suspicious_changes": [_suspect_to_dict(s) for s in heuristic_suspects],
                "note": (
                    "The confirmed_regression field above is sourced from an isolated replay "
                    "AUTOPSY ran itself (test suite executed with and without the commit) — "
                    "a genuine result, not a static-heuristic guess. The suspicious_changes "
                    "list below is still the same PLAUSIBLE-trigger heuristic ranking as "
                    "always, included for context."
                ),
            }
        else:
            # A real result, just a negative one: replay ran successfully
            # but reverting this commit did NOT remove the failure. That's
            # worth surfacing distinctly from "we never checked" — it
            # actively rules this commit out, which is useful even though
            # it isn't a confirmed regression.
            return {
                "status": "insufficient_data",
                "message": (
                    f"Counterfactual replay ran successfully but reverting commit "
                    f"{counterfactual_result.short_sha} did not eliminate the observed "
                    f"failure — this commit is likely not the cause."
                ),
                "ruled_out": _counterfactual_to_dict(counterfactual_result),
                "suspicious_changes": [_suspect_to_dict(s) for s in heuristic_suspects],
                "note": (
                    "The ruled_out field reflects a real replay result, not a heuristic. "
                    "The suspicious_changes list below is the same PLAUSIBLE-trigger "
                    "heuristic ranking as always."
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
    # Reserved for future work: has_test_execution_data True but neither
    # confirmed_regression nor counterfactual_result provided shouldn't
    # normally happen given how pipeline.py/the counterfactual endpoint
    # set it — kept as an explicit error rather than silently falling
    # through, so a future caller mismatch is loud instead of quietly
    # returning insufficient_data.
    raise NotImplementedError(
        "has_test_execution_data=True but no confirmed_regression or "
        "counterfactual_result was provided."
    )


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
