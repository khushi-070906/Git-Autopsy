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

import networkx as nx

from app.analysis.dependency_parser import diff_dependency_manifest, _is_dev_dependency

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


def _score_dependency_signal(
    g: nx.MultiDiGraph, commit_node: str, changed_deps: list[tuple[str, dict]]
) -> tuple[float, list[EvidenceItem], list[str]]:
    """
    Signal 1: dependency manifest touched.

    Fix: previously this scored purely on "a file named package.json was
    touched", so a commit that only bumped a dev-tool like `prettier`
    scored identically to one that bumped a runtime dependency like
    `torch`. This now actually diffs the manifest content at this commit
    vs. its parent (via git blob reads — no checkout, no execution) and
    classifies each changed package as dev-only vs. runtime, weighting the
    signal accordingly. Runtime dependency changes are a real regression
    risk; dev-tooling changes (formatters, linters) essentially never are.
    """
    score = 0.0
    evidence: list[EvidenceItem] = []
    reasons: list[str] = []

    if not changed_deps:
        return score, evidence, reasons

    repo_path = g.graph.get("repo_path")
    commit_sha = g.nodes[commit_node].get("sha")
    parents = g.nodes[commit_node].get("parents") or []
    parent_sha = parents[0] if parents else None

    runtime_changed: list[str] = []
    dev_changed: list[str] = []

    if repo_path and commit_sha:
        for manifest_node, _ in changed_deps:
            manifest_path = g.nodes[manifest_node].get("path", manifest_node)
            try:
                diff = diff_dependency_manifest(repo_path, commit_sha, parent_sha, manifest_path)
            except Exception:
                # Never let a blob-read failure crash scoring — fall back to
                # the old coarse behavior (treat as an unclassified runtime
                # change) for this manifest only.
                dep_names = [g.nodes[v].get("path", v) for v, _ in changed_deps]
                runtime_changed.extend(dep_names)
                continue
            for dep in diff["added"] + diff["removed"] + diff["changed"]:
                target = dev_changed if _is_dev_dependency(dep) else runtime_changed
                target.append(dep["name"])
    else:
        # No repo_path on the graph (e.g. an older graph built before this
        # fix, or a test double) — fall back to the old coarse behavior
        # rather than silently under-scoring.
        dep_names = [g.nodes[v].get("path", v) for v, _ in changed_deps]
        runtime_changed = dep_names

    runtime_changed = list(dict.fromkeys(runtime_changed))
    dev_changed = list(dict.fromkeys(dev_changed))

    if runtime_changed:
        score += 0.35
        evidence.append(EvidenceItem(
            "FACT", f"Runtime dependency changed: {', '.join(runtime_changed)}"
        ))
        reasons.append("a dependency manifest change")
    elif dev_changed:
        # Dev/build tooling (formatters, linters, type-checkers) almost
        # never causes a runtime behavioral regression on its own.
        score += 0.05
        evidence.append(EvidenceItem(
            "FACT", f"Dev-only dependency changed: {', '.join(dev_changed)}"
        ))

    return score, evidence, reasons


def _score_commit(
    g: nx.MultiDiGraph, commit_node: str
) -> tuple[float, list[EvidenceItem], str]:
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

    # Signal 1: dependency manifest touched (see _score_dependency_signal).
    dep_score, dep_evidence, dep_reasons = _score_dependency_signal(g, commit_node, changed_deps)
    score += dep_score
    evidence.extend(dep_evidence)
    reasons.extend(dep_reasons)

    # Signal 2: large diff (many insertions/deletions) — bigger surface area
    # for introducing unintended behavior change.
    #
    # Fix: a commit that is almost entirely *new* files (scaffolding —
    # initial commits, "add CI/docs/docker" commits) trivially maximizes
    # this signal without containing any modification to existing behavior.
    # A regression by definition changes existing behavior, so we dampen
    # the weight for diffs that are overwhelmingly additive-to-new-files.
    total_insertions = sum(d.get("insertions", 0) for _, d in changed_files)
    total_deletions = sum(d.get("deletions", 0) for _, d in changed_files)
    total_churn = total_insertions + total_deletions
    new_file_count = sum(1 for _, d in changed_files if d.get("change_type") == "A")
    is_scaffolding = bool(changed_files) and (
        new_file_count / len(changed_files) > 0.8
        and total_deletions < 0.1 * (total_insertions + 1)
    )

    if total_churn > 200:
        weight = 0.05 if is_scaffolding else 0.15
        score += weight
        note = " (mostly new files — looks like scaffolding, not a modification)" if is_scaffolding else ""
        evidence.append(EvidenceItem(
            "FACT",
            f"Large diff: {total_churn} lines changed across {len(changed_files)} files{note}",
        ))
        reasons.append("an unusually large change")
    elif total_churn > 50:
        score += 0.03 if is_scaffolding else 0.07

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


def rank_suspects(
    g: nx.MultiDiGraph,
    top_n: int = 5,
    min_confidence: float = 0.2,
    has_test_framework: bool = True,
) -> list[Suspect]:
    """
    Score every commit in the graph and return the top-N ranked by
    confidence, each carrying its own transparent evidence chain.

    `has_test_framework`: when False (no test framework detected in the
    repo at all — not even executed, just not present), the report has
    zero ground truth to validate any of these heuristics against. Rather
    than let the static score alone produce a number like "75% confidence"
    that reads as near-certain, confidence is capped so the UI can't
    overclaim on repos we have the least evidence about.
    """
    commit_nodes = [n for n, d in g.nodes(data=True) if d.get("kind") == "commit"]
    scored: list[Suspect] = []

    for cn in commit_nodes:
        score, evidence, summary = _score_commit(g, cn)

        if not has_test_framework:
            score = min(score, 0.40)

        if score < min_confidence:
            continue

        data = g.nodes[cn]
        affected_files = [
            g.nodes[v].get("path", v)
            for _, v, d in g.out_edges(cn, data=True)
            if d.get("kind") == "COMMIT_CHANGED_FILE"
        ]
        # Fix: put dependency manifests first when they're present, since
        # they're what the "likely cause" text and evidence actually name —
        # previously the displayed file list could be a slice of the diff
        # unrelated to the manifest the score/summary referenced.
        affected_files.sort(key=lambda f: 0 if f in DEPENDENCY_MANIFESTS else 1)

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
