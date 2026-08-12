"""
Phase 4 — WHY analysis.

This is the deterministic causal-reasoning core. It never invents evidence:
every EvidenceItem here is traced back to a concrete fact already present in
the Evidence Graph (a commit, a diff, a dependency change, a test file
association). Confidence is a transparent, formula-derived score — not a
model's opinion — so it can be explained and reproduced.

Classification discipline (required by spec):
  FACT           - directly observed in git history / source (e.g. "file X
                   changed in commit Y").
  EVIDENCE       - a fact used in support of a hypothesis.
  INFERENCE      - a conclusion drawn from a pattern of evidence, never
                   claimed as certain.
  RECOMMENDATION - a concrete next investigative step for the developer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import networkx as nx

DEPENDENCY_MANIFESTS = {
    "requirements.txt", "pyproject.toml", "package.json",
    "package-lock.json", "poetry.lock", "Cargo.toml", "go.mod",
}


@dataclass
class EvidenceItem:
    kind: str  # "FACT" | "EVIDENCE" | "INFERENCE" | "RECOMMENDATION"
    text: str


@dataclass
class Suspect:
    commit_sha: str
    short_sha: str
    message: str
    author: str
    date: str
    confidence: float  # 0-1
    likely_cause_summary: str
    evidence: list[EvidenceItem] = field(default_factory=list)
    affected_files: list[str] = field(default_factory=list)
    affected_functions: list[str] = field(default_factory=list)
    recommendation: str = ""


def _score_commit(g: nx.MultiDiGraph, commit_node: str) -> tuple[float, list[EvidenceItem], str]:
    """
    Score a single commit as a regression suspect using transparent,
    additive signals. Each signal that fires appends an EvidenceItem so the
    final confidence number is fully explainable, not a black box.
    """
    score = 0.0
    evidence: list[EvidenceItem] = []
    reasons = []

    out_edges = list(g.out_edges(commit_node, data=True))
    changed_files = [
        (v, d) for _, v, d in out_edges if d.get("kind") == "COMMIT_CHANGED_FILE"
    ]
    changed_deps = [
        (v, d) for _, v, d in out_edges if d.get("kind") == "COMMIT_CHANGED_DEPENDENCY"
    ]

    # Signal 1: dependency manifest touched — historically a common
    # regression source (breaking API/behavior changes in third-party code).
    if changed_deps:
        score += 0.35
        dep_names = [g.nodes[v].get("path", v) for v, _ in changed_deps]
        evidence.append(EvidenceItem("FACT", f"Dependency manifest changed: {', '.join(dep_names)}"))
        reasons.append("a dependency manifest change")

    # Signal 2: large diff (many insertions/deletions) — bigger surface area
    # for introducing unintended behavior change.
    total_churn = sum(d.get("insertions", 0) + d.get("deletions", 0) for _, d in changed_files)
    if total_churn > 200:
        score += 0.15
        evidence.append(EvidenceItem("FACT", f"Large diff: {total_churn} lines changed across {len(changed_files)} files"))
        reasons.append("an unusually large change")
    elif total_churn > 50:
        score += 0.07

    # Signal 3: commit message contains risk-signaling keywords.
    message = g.nodes[commit_node].get("message", "").lower()
    risk_keywords = ["upgrade", "bump", "update dep", "fix", "revert", "hack", "workaround", "hotfix"]
    hit_keywords = [k for k in risk_keywords if k in message]
    if hit_keywords:
        score += 0.15
        evidence.append(EvidenceItem("FACT", f"Commit message contains risk-signaling terms: {', '.join(hit_keywords)}"))
        reasons.append("wording in the commit message")

    # Signal 4: touches files that are known to feed tests (higher blast
    # radius / more likely to be caught, but also more likely a genuine
    # behavioral change if it precedes failures).
    tested_functions_touched = 0
    for fpath, _ in changed_files:
        for _, fn, edata in g.out_edges(fpath, data=True):
            if edata.get("kind") == "FILE_CONTAINS_FUNCTION":
                if any(d.get("kind") == "FUNCTION_USED_BY_TEST" for _, _, d in g.out_edges(fn, data=True)):
                    tested_functions_touched += 1
    if tested_functions_touched:
        score += min(0.25, 0.05 * tested_functions_touched)
        evidence.append(EvidenceItem(
            "FACT",
            f"Changed code touches {tested_functions_touched} function(s) referenced by tests",
        ))
        reasons.append("changes to test-covered functions")

    # Signal 5: authored by a single-commit contributor touching core files
    # (weak signal, small weight) — omitted from MVP scoring to avoid
    # unfounded bias against new contributors; left as future work.

    score = min(score, 0.97)  # never claim near-certainty from heuristics alone

    if reasons:
        summary = "Likely cause: " + " combined with ".join(reasons) + "."
    else:
        summary = "No strong regression signals detected for this commit."

    return score, evidence, summary


def rank_suspects(g: nx.MultiDiGraph, top_n: int = 5, min_confidence: float = 0.2) -> list[Suspect]:
    """
    Score every commit in the graph and return the top-N ranked by
    confidence, each carrying its own transparent evidence chain.
    """
    commit_nodes = [n for n, d in g.nodes(data=True) if d.get("kind") == "commit"]
    scored: list[Suspect] = []

    for cn in commit_nodes:
        score, evidence, summary = _score_commit(g, cn)
        if score < min_confidence:
            continue

        data = g.nodes[cn]
        affected_files = [
            g.nodes[v].get("path", v)
            for _, v, d in g.out_edges(cn, data=True)
            if d.get("kind") == "COMMIT_CHANGED_FILE"
        ]
        affected_functions = []
        for fpath in affected_files:
            file_id = f"file:{fpath}"
            if file_id in g:
                for _, fn, d in g.out_edges(file_id, data=True):
                    if d.get("kind") == "FILE_CONTAINS_FUNCTION":
                        affected_functions.append(g.nodes[fn].get("name", fn))

        evidence.append(EvidenceItem(
            "INFERENCE",
            f"Based on the signals above, this commit is a plausible regression source "
            f"(confidence {round(score * 100)}%). This is a statistical inference from "
            f"available evidence, not a proven root cause.",
        ))

        recommendation = _build_recommendation(g, cn, affected_files)
        evidence.append(EvidenceItem("RECOMMENDATION", recommendation))

        scored.append(Suspect(
            commit_sha=data["sha"],
            short_sha=data["short_sha"],
            message=data["message"].splitlines()[0][:200] if data["message"] else "(no message)",
            author=data["author"],
            date=data["date"],
            confidence=round(score, 2),
            likely_cause_summary=summary,
            evidence=evidence,
            affected_files=affected_files,
            affected_functions=list(dict.fromkeys(affected_functions))[:10],
            recommendation=recommendation,
        ))

    scored.sort(key=lambda s: s.confidence, reverse=True)
    return scored[:top_n]


def _build_recommendation(g: nx.MultiDiGraph, commit_node: str, affected_files: list[str]) -> str:
    dep_touched = any(f in DEPENDENCY_MANIFESTS for f in affected_files)
    if dep_touched:
        return (
            f"Diff the dependency manifest between this commit and its parent to see exactly "
            f"which package versions changed, then check each package's changelog for "
            f"behavior-affecting changes."
        )
    if affected_files:
        sample = ", ".join(affected_files[:3])
        return f"Review the diff for {sample} against the parent commit and re-run any tests covering those files."
    return "Review this commit's full diff manually; no specific file signal was strong enough to narrow the search further."
