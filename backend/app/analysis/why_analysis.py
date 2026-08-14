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

# Extensions that never indicate a runtime-behavior-affecting change on
# their own — used to gate the "fix" keyword (see Signal 3) so a docs-only
# commit that happens to say "fix the issue template" doesn't get treated
# the same as a commit that says "fix null pointer in parser".
NON_RISK_EXTENSIONS = {".md", ".txt", ".rst"}


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

    Actually diffs the manifest content at this commit vs. its parent (via
    git blob reads — no checkout, no execution) and classifies each changed
    package as dev-only vs. runtime, weighting the signal accordingly.
    Runtime dependency changes are a real regression risk; dev-tooling
    changes (formatters, linters) essentially never are.
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
                dep_names = [g.nodes[v].get("path", v) for v, _ in changed_deps]
                runtime_changed.extend(dep_names)
                continue
            for dep in diff["added"] + diff["removed"] + diff["changed"]:
                target = dev_changed if _is_dev_dependency(dep) else runtime_changed
                target.append(dep["name"])
    else:
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

    # Signal 2: large diff, dampened for scaffolding-style commits.
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
    #
    # Fix: "fix" alone is too noisy — it fires on purely doc/template
    # commits (e.g. "Fix .github/ISSUE_TEMPLATE from bare file to proper
    # directory") that have nothing to do with a code regression. Only
    # count "fix" as a risk signal if the commit also touches a file that
    # isn't just docs/text — every other keyword here (revert, hack,
    # workaround, hotfix, upgrade, bump) is specific enough to keep as-is.
    message = g.nodes[commit_node].get("message", "").lower()
    always_risky_keywords = ["upgrade", "bump", "update dep", "revert", "hack", "workaround", "hotfix"]
    hit_keywords = [k for k in always_risky_keywords if k in message]

    if "fix" in message:
        non_doc_touched = any(
            not any(g.nodes[v].get("path", v).endswith(ext) for ext in NON_RISK_EXTENSIONS)
            for v, _ in changed_files
        )
        if non_doc_touched:
            hit_keywords.append("fix")

    if hit_keywords:
        score += 0.15
        evidence.append(EvidenceItem("FACT", f"Commit message contains risk-signaling terms: {', '.join(hit_keywords)}"))
        reasons.append("wording in the commit message")

    # Signal 4: touches files that are known to feed tests.
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
    has_weak_test_signal: bool = False,
) -> list[Suspect]:
    """
    Score every commit in the graph and return the top-N ranked by
    confidence, each carrying its own transparent evidence chain.

    `has_test_framework`: True only when a real, named test framework was
    detected (pytest, jest, etc.) — not just a bare `tests/` directory.
    When False, confidence is capped since there's no real ground truth to
    validate the static heuristics against.

    `has_weak_test_signal`: True when detect_test_framework() found only a
    generic test directory ("unknown (test directory present)") with no
    actual framework markers. This is weaker evidence than a real
    framework but still more than nothing, so it gets a looser cap (55%)
    than the no-signal-at-all case (40%).
    """
    commit_nodes = [n for n, d in g.nodes(data=True) if d.get("kind") == "commit"]
    scored: list[Suspect] = []

    for cn in commit_nodes:
        score, evidence, summary = _score_commit(g, cn)

        if not has_test_framework:
            cap = 0.55 if has_weak_test_signal else 0.40
            score = min(score, cap)

        if score < min_confidence:
            continue

        data = g.nodes[cn]
        affected_files = [
            g.nodes[v].get("path", v)
            for _, v, d in g.out_edges(cn, data=True)
            if d.get("kind") == "COMMIT_CHANGED_FILE"
        ]
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
