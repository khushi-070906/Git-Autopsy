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

from app.analysis.dependency_parser import (
    classify_version_bump,
    diff_dependency_manifest,
    _is_dev_dependency,
)
from app.analysis.git_history import get_file_content_at_commit
from app.analysis.static_python import analyze_python_source

DEPENDENCY_MANIFESTS = {
    "requirements.txt", "pyproject.toml", "package.json",
    "package-lock.json", "poetry.lock", "Cargo.toml", "go.mod",
}

NON_RISK_EXTENSIONS = {".md", ".txt", ".rst"}

# Weight applied per bump severity when a runtime dependency's *version*
# changes (as opposed to being freshly added/removed). A major bump is far
# more likely to introduce a breaking behavior change than a patch bump.
BUMP_SEVERITY_WEIGHT = {
    "major": 0.35,
    "minor": 0.22,
    "patch": 0.10,
    "same": 0.0,
    "unknown": 0.25,  # can't tell — treat as moderately risky, not zero
}

# A file touched this many times or more across the reviewed commit history
# is a "hotspot" — historically more prone to change, so a further change
# to it is treated as a (small) elevated risk relative to a file touched
# once. This is a coarse heuristic, not a claim of causation.
HOTSPOT_THRESHOLD = 5

# An author with this many or fewer commits in the reviewed history is
# treated as a newer contributor for the purposes of Signal 6. Deliberately
# small and additive-only (not punitive) — the goal is a mild signal, not
# bias against new contributors.
NEW_CONTRIBUTOR_COMMIT_THRESHOLD = 1


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


def _compute_repo_stats(g: nx.MultiDiGraph) -> dict:
    """
    One pass over the whole graph to build aggregate stats used by
    per-commit signals that need history-wide context (file churn counts,
    per-author commit counts) rather than just this-commit-only facts.
    Computed once per rank_suspects() call, not once per commit.
    """
    file_churn: dict[str, int] = {}
    author_counts: dict[str, int] = {}

    for _, d in g.nodes(data=True):
        if d.get("kind") == "commit":
            author = d.get("author", "unknown")
            author_counts[author] = author_counts.get(author, 0) + 1

    for _, v, d in g.edges(data=True):
        if d.get("kind") == "COMMIT_CHANGED_FILE":
            fpath = g.nodes[v].get("path", v)
            file_churn[fpath] = file_churn.get(fpath, 0) + 1

    return {"file_churn": file_churn, "author_counts": author_counts}


def _score_dependency_signal(
    g: nx.MultiDiGraph, commit_node: str, changed_deps: list[tuple[str, dict]]
) -> tuple[float, list[EvidenceItem], list[str]]:
    """
    Signal 1: dependency manifest touched.

    Diffs the manifest content at this commit vs. its parent, classifies
    each changed package as dev-only vs. runtime, and — new in this round —
    for packages whose *version* changed (not just added/removed), weights
    by bump severity (major/minor/patch) via classify_version_bump(). A
    freshly added or removed runtime dependency gets a flat moderate
    weight, since "added" can't by itself have changed behavior of code
    that doesn't use it yet (Signal 4 separately catches whether the diff
    also touches code that consumes it).
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

    runtime_changed_versions: list[tuple[str, str]] = []  # (name, bump_severity)
    runtime_added_removed: list[str] = []
    dev_changed: list[str] = []

    if repo_path and commit_sha:
        for manifest_node, _ in changed_deps:
            manifest_path = g.nodes[manifest_node].get("path", manifest_node)
            try:
                diff = diff_dependency_manifest(repo_path, commit_sha, parent_sha, manifest_path)
            except Exception:
                dep_names = [g.nodes[v].get("path", v) for v, _ in changed_deps]
                runtime_added_removed.extend(dep_names)
                continue

            for dep in diff["changed"]:
                if _is_dev_dependency(dep):
                    dev_changed.append(dep["name"])
                else:
                    bump = classify_version_bump(dep.get("old_version", ""), dep.get("version", ""))
                    runtime_changed_versions.append((dep["name"], bump))

            for dep in diff["added"] + diff["removed"]:
                if _is_dev_dependency(dep):
                    dev_changed.append(dep["name"])
                else:
                    runtime_added_removed.append(dep["name"])
    else:
        dep_names = [g.nodes[v].get("path", v) for v, _ in changed_deps]
        runtime_added_removed = dep_names

    dev_changed = list(dict.fromkeys(dev_changed))
    runtime_added_removed = list(dict.fromkeys(runtime_added_removed))

    if runtime_changed_versions:
        # Worst-case bump drives the score — one major bump among several
        # patch bumps is still the thing worth investigating first.
        worst_name, worst_bump = max(
            runtime_changed_versions, key=lambda item: BUMP_SEVERITY_WEIGHT.get(item[1], 0.0)
        )
        score += BUMP_SEVERITY_WEIGHT.get(worst_bump, 0.25)
        names = ", ".join(n for n, _ in runtime_changed_versions)
        severity_note = f" (worst: {worst_name} — {worst_bump} version bump)" if worst_bump != "unknown" else ""
        evidence.append(EvidenceItem(
            "FACT", f"Runtime dependency version changed: {names}{severity_note}"
        ))
        reasons.append("a dependency version change")

    if runtime_added_removed:
        score += 0.20
        evidence.append(EvidenceItem(
            "FACT", f"Runtime dependency added/removed: {', '.join(runtime_added_removed)}"
        ))
        if not reasons:
            reasons.append("a dependency manifest change")

    if dev_changed and not runtime_changed_versions and not runtime_added_removed:
        score += 0.05
        evidence.append(EvidenceItem(
            "FACT", f"Dev-only dependency changed: {', '.join(dev_changed)}"
        ))

    return score, evidence, reasons


def _parent_and_current_analysis(
    g: nx.MultiDiGraph, commit_node: str, path: str
) -> tuple["FileAnalysis | None", "FileAnalysis | None"]:  # noqa: F821
    """
    Parse a Python file's content at this commit and at its parent, via git
    blob reads (no checkout). Returns (None, None) if either read fails, the
    file didn't exist at one side, or repo_path/parent isn't known — callers
    treat that as "nothing to compare" and skip.
    """
    repo_path = g.graph.get("repo_path")
    commit_sha = g.nodes[commit_node].get("sha")
    parents = g.nodes[commit_node].get("parents") or []
    parent_sha = parents[0] if parents else None
    if not repo_path or not commit_sha or not parent_sha:
        return None, None

    curr_text = get_file_content_at_commit(repo_path, commit_sha, path)
    prev_text = get_file_content_at_commit(repo_path, parent_sha, path)
    if curr_text is None or prev_text is None:
        return None, None

    try:
        curr_fa = analyze_python_source(curr_text, path)
        prev_fa = analyze_python_source(prev_text, path)
    except Exception:
        return None, None

    if curr_fa.parse_error or prev_fa.parse_error:
        return None, None

    return prev_fa, curr_fa


def _score_removed_guard_signal(
    g: nx.MultiDiGraph, commit_node: str, changed_files: list[tuple[str, dict]]
) -> tuple[float, list[EvidenceItem], list[str]]:
    """
    Signal 8: a function lost validation logic (a `raise` or `assert`)
    between this commit and its parent, with nothing added back in its
    place. A raw line-count diff (Signal 2) can't tell "removed a guard
    clause" from "removed a comment" — this compares parsed function bodies
    on each side, so it's specific to actual control-flow loss.

    Deliberately conservative: only fires when a function present on *both*
    sides has a lower guard_count on the new side. A function removed
    entirely is a different (much louder) signal and isn't double-counted
    here.
    """
    score = 0.0
    evidence: list[EvidenceItem] = []
    reasons: list[str] = []

    dropped: list[tuple[str, str, int, int]] = []
    for fnode, fdata in changed_files:
        path = g.nodes[fnode].get("path", fnode)
        if fdata.get("change_type") != "M" or not path.endswith(".py"):
            continue
        prev_fa, curr_fa = _parent_and_current_analysis(g, commit_node, path)
        if prev_fa is None or curr_fa is None:
            continue

        curr_by_name = {fn.name: fn for fn in curr_fa.functions}
        for prev_fn in prev_fa.functions:
            curr_fn = curr_by_name.get(prev_fn.name)
            if curr_fn is None:
                continue
            if curr_fn.guard_count < prev_fn.guard_count:
                dropped.append((prev_fn.name, path, prev_fn.guard_count, curr_fn.guard_count))

    if dropped:
        score += min(0.30, 0.15 * len(dropped))
        sample = ", ".join(
            f"{name}() in {path} ({before}\u2192{after} guard statements)"
            for name, path, before, after in dropped[:3]
        )
        evidence.append(EvidenceItem(
            "FACT",
            f"Function(s) lost validation logic (raise/assert count decreased, nothing "
            f"added back): {sample}",
        ))
        reasons.append("removed input-validation logic")

    return score, evidence, reasons


def _score_signature_break_signal(
    g: nx.MultiDiGraph, commit_node: str, changed_files: list[tuple[str, dict]]
) -> tuple[float, list[EvidenceItem], list[str]]:
    """
    Signal 9: a function's parameter order changed in this commit, and a
    real call site elsewhere in the repo (found via CALLS edges — actual
    positional-argument-count matching, not name matching) still calls it
    positionally with the old argument count, in a file this commit never
    touched. This is the sharpest structural signal AUTOPSY can raise
    without executing code: the call site will silently receive arguments
    in the wrong slots.

    Scoped narrowly on purpose: only fires when the parameter *set* is
    identical and just the *order* changed (a same-name reorder), since
    that's the case that's silent — a renamed or added/removed parameter
    would usually be a TypeError at call time, which is a different (and
    louder, test-suite-visible) failure mode already partly covered by
    Signal 4.
    """
    score = 0.0
    evidence: list[EvidenceItem] = []
    reasons: list[str] = []

    changed_file_paths = {g.nodes[v].get("path", v) for v, _ in changed_files}
    breaks: list[tuple[str, str, list[str], list[str], str, str]] = []

    for fnode, fdata in changed_files:
        path = g.nodes[fnode].get("path", fnode)
        if fdata.get("change_type") != "M" or not path.endswith(".py"):
            continue
        prev_fa, curr_fa = _parent_and_current_analysis(g, commit_node, path)
        if prev_fa is None or curr_fa is None:
            continue

        curr_by_name = {fn.name: fn for fn in curr_fa.functions}
        for prev_fn in prev_fa.functions:
            curr_fn = curr_by_name.get(prev_fn.name)
            if curr_fn is None or curr_fn.args == prev_fn.args:
                continue
            if set(curr_fn.args) != set(prev_fn.args):
                continue  # not a pure reorder — different, louder failure mode

            fn_id = f"function:{path}::{prev_fn.name}"
            if fn_id not in g:
                continue

            for caller_id, _, edata in g.in_edges(fn_id, data=True):
                if edata.get("kind") != "CALLS" or edata.get("keyword_args"):
                    continue
                if edata.get("arg_count") != len(prev_fn.args):
                    continue
                caller_data = g.nodes[caller_id]
                caller_file = caller_data.get("file", "")
                if caller_file in changed_file_paths:
                    continue  # caller was updated in this same commit
                breaks.append((
                    prev_fn.name, path, prev_fn.args, curr_fn.args,
                    caller_data.get("name", "?"), caller_file,
                ))

    if breaks:
        score += min(0.45, 0.35 + 0.05 * (len(breaks) - 1))
        name, path, old_args, new_args, caller_name, caller_file = breaks[0]
        evidence.append(EvidenceItem(
            "FACT",
            f"{name}() in {path} had its parameter order changed "
            f"({', '.join(old_args)} \u2192 {', '.join(new_args)}), but {caller_name}() in "
            f"{caller_file} still calls it positionally with the old argument count and "
            f"was not touched in this commit — its arguments will silently land in the "
            f"wrong slots.",
        ))
        reasons.append("a function signature change with an un-updated positional caller")

    return score, evidence, reasons


def _score_commit(
    g: nx.MultiDiGraph, commit_node: str, repo_stats: dict
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
    #
    # Fix: pure renames (change_type == "R") are excluded from churn —
    # a file move/rename can carry large insertion/deletion counts from
    # git's similarity detection without any real behavioral change, and
    # previously wasn't counted as "new" either, so it got neither the
    # rename discount nor the scaffolding discount.
    non_rename_files = [(v, d) for v, d in changed_files if d.get("change_type") != "R"]
    total_insertions = sum(d.get("insertions", 0) for _, d in non_rename_files)
    total_deletions = sum(d.get("deletions", 0) for _, d in non_rename_files)
    total_churn = total_insertions + total_deletions
    new_file_count = sum(1 for _, d in non_rename_files if d.get("change_type") == "A")
    is_scaffolding = bool(non_rename_files) and (
        new_file_count / len(non_rename_files) > 0.8
        and total_deletions < 0.1 * (total_insertions + 1)
    )

    if total_churn > 200:
        weight = 0.05 if is_scaffolding else 0.15
        score += weight
        note = " (mostly new files — looks like scaffolding, not a modification)" if is_scaffolding else ""
        evidence.append(EvidenceItem(
            "FACT",
            f"Large diff: {total_churn} lines changed across {len(non_rename_files)} files{note}",
        ))
        reasons.append("an unusually large change")
    elif total_churn > 50:
        score += 0.03 if is_scaffolding else 0.07

    # Signal 3: commit message contains risk-signaling keywords, with "fix"
    # gated to commits that touch non-doc files.
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

    # Signal 5: file hotspot — a touched file with a long history of
    # changes across the reviewed commits is more likely to be a source
    # of instability than a file touched once ever. Small weight; this is
    # a correlational signal, not a causal one.
    file_churn = repo_stats["file_churn"]
    hotspot_files = [
        g.nodes[v].get("path", v) for v, _ in changed_files
        if file_churn.get(g.nodes[v].get("path", v), 0) >= HOTSPOT_THRESHOLD
    ]
    if hotspot_files:
        score += 0.08
        sample = ", ".join(hotspot_files[:3])
        evidence.append(EvidenceItem(
            "FACT",
            f"Touches file(s) with a long change history in this repo: {sample} "
            f"({HOTSPOT_THRESHOLD}+ prior changes)",
        ))
        reasons.append("changes to a historically volatile file")

    # Signal 6: author is new to this repo (few or no other commits in the
    # reviewed history) and is touching non-doc files. Small, additive-only
    # weight — not meant to penalize new contributors, just to reflect that
    # unfamiliarity with a codebase is a mild statistical risk factor.
    author = g.nodes[commit_node].get("author", "unknown")
    author_commit_count = repo_stats["author_counts"].get(author, 1)
    touches_non_doc = any(
        not any(g.nodes[v].get("path", v).endswith(ext) for ext in NON_RISK_EXTENSIONS)
        for v, _ in changed_files
    )
    if author_commit_count <= NEW_CONTRIBUTOR_COMMIT_THRESHOLD and touches_non_doc and not is_scaffolding:
        score += 0.05
        evidence.append(EvidenceItem(
            "FACT",
            f"Author has {author_commit_count} commit(s) in the reviewed history",
        ))
        reasons.append("limited commit history from this author in this repo")

    # Signal 7: merge-commit dampening. A merge commit's diff typically
    # represents the union of already-reviewed branch commits (each of
    # which is scored independently elsewhere in this same pass) — scoring
    # the merge itself at full weight double-counts that risk and adds
    # noise. Dampen rather than exclude entirely, since a merge commit can
    # still occasionally introduce its own conflict-resolution bugs.
    parents = g.nodes[commit_node].get("parents") or []
    is_merge = len(parents) > 1
    if is_merge and score > 0:
        pre_dampen_score = score
        score *= 0.3
        evidence.append(EvidenceItem(
            "FACT",
            f"This is a merge commit ({len(parents)} parents) — signals below likely reflect "
            f"already-reviewed branch commits rather than new risk introduced here "
            f"(raw signal score {round(pre_dampen_score * 100)}% dampened to {round(score * 100)}%)",
        ))

    score = min(score, 0.97)  # never claim near-certainty from heuristics alone

    if reasons:
        summary = "Likely cause: " + " combined with ".join(reasons) + "."
        if is_merge:
            summary += " (dampened — merge commit)"
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
    """
    commit_nodes = [n for n, d in g.nodes(data=True) if d.get("kind") == "commit"]
    repo_stats = _compute_repo_stats(g)
    scored: list[Suspect] = []

    for cn in commit_nodes:
        score, evidence, summary = _score_commit(g, cn, repo_stats)

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
