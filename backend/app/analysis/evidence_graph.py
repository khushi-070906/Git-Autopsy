"""
Phase 3 — The Evidence Graph.

This is AUTOPSY's core technical idea: every fact the analysis engine
discovers becomes a typed node, and every relationship between facts becomes
a typed, evidenced edge. The WHY analysis (Phase 4) reasons over this graph
instead of re-deriving facts or asking an LLM to guess.

Node kinds: commit, file, function, dependency, test, failure, author, change
Edge kinds: COMMIT_CHANGED_FILE, COMMIT_CHANGED_DEPENDENCY,
            FILE_CONTAINS_FUNCTION, FUNCTION_USED_BY_TEST,
            COMMIT_PRECEDED_FAILURE, FILE_DEPENDS_ON_PACKAGE,
            COMMIT_AUTHORED_BY, CALLS, IMPORTS
"""
from __future__ import annotations

from pathlib import Path

import networkx as nx

from app.analysis.dependency_parser import parse_dependency_file
from app.analysis.detect import detect_dependency_files
from app.analysis.git_history import CommitRecord
from app.analysis.static_python import FileAnalysis


def _module_name_for_path(rel_path: str) -> str:
    """
    Convert a repo-relative .py file path to its dotted module name, e.g.
    'app/analysis/git_history.py' -> 'app.analysis.git_history',
    'app/__init__.py' -> 'app'.
    """
    p = rel_path[:-3] if rel_path.endswith(".py") else rel_path
    parts = p.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_local_imports(file_analyses: list[FileAnalysis]) -> dict[str, set[str]]:
    """
    Best-effort resolution of each Python file's imports to other files in
    *this* repo — most imports are external packages and simply won't
    match, which is expected and fine; only in-repo edges are returned.

    Matching is exact-module-path first (`import app.analysis.git_history`),
    then a same-tail-segment fallback (`mod_name.endswith("." + imp)`) to
    catch relative imports like `from .git_history import X` — ast records
    the module as just "git_history", not the package-qualified path.
    Same name-based-heuristic spirit as CALLS/FUNCTION_USED_BY_TEST
    elsewhere in this file: two same-named modules in unrelated packages
    could collide. Acceptable for a "here's the rough shape of the
    codebase" graph, not a claim of perfect resolution.
    """
    module_to_path: dict[str, str] = {}
    for fa in file_analyses:
        if fa.path.endswith(".py"):
            module_to_path[_module_name_for_path(fa.path)] = fa.path

    result: dict[str, set[str]] = {}
    for fa in file_analyses:
        targets: set[str] = set()
        for imp in fa.imports:
            direct = module_to_path.get(imp)
            if direct and direct != fa.path:
                targets.add(direct)
                continue
            for mod_name, path in module_to_path.items():
                if path == fa.path:
                    continue
                if mod_name == imp or mod_name.endswith("." + imp):
                    targets.add(path)
                    break
        if targets:
            result[fa.path] = targets
    return result


def build_evidence_graph(
    repo_path: Path,
    commits: list[CommitRecord],
    file_analyses: list[FileAnalysis],
) -> nx.MultiDiGraph:
    """
    Build the Evidence Graph from extracted git history and static analysis.
    Returns a NetworkX MultiDiGraph (multiple typed edges between the same
    pair of nodes are allowed and meaningful — e.g. a file can be changed by
    many commits).
    """
    g = nx.MultiDiGraph()

    # Stash the repo path on the graph itself. WHY analysis (Signal 1) needs
    # to re-read manifest file contents at specific commits (via
    # dependency_parser.diff_dependency_manifest) to know exactly which
    # package changed in a given commit, rather than just "a file called
    # package.json was touched" — so it needs a way back to the checkout.
    g.graph["repo_path"] = str(repo_path)

    # --- Commit and author nodes -----------------------------------------
    for c in commits:
        g.add_node(
            f"commit:{c.sha}",
            kind="commit",
            sha=c.sha,
            short_sha=c.short_sha,
            author=c.author,
            date=c.date,
            message=c.message,
            # Needed so WHY analysis can diff a manifest against the right
            # parent commit instead of only knowing the file was touched.
            parents=c.parents,
        )
        author_id = f"author:{c.author_email or c.author}"
        if author_id not in g:
            g.add_node(author_id, kind="author", name=c.author, email=c.author_email)
        g.add_edge(f"commit:{c.sha}", author_id, kind="COMMIT_AUTHORED_BY")

        for fc in c.files_changed:
            file_id = f"file:{fc.path}"
            if file_id not in g:
                g.add_node(file_id, kind="file", path=fc.path)
            g.add_edge(
                f"commit:{c.sha}",
                file_id,
                kind="COMMIT_CHANGED_FILE",
                change_type=fc.change_type,
                insertions=fc.insertions,
                deletions=fc.deletions,
            )

            # Dependency-manifest changes get a distinct edge kind so WHY
            # analysis can specifically flag dependency-driven regressions.
            if fc.path in {
                "requirements.txt", "pyproject.toml", "package.json",
                "package-lock.json", "poetry.lock", "Cargo.toml", "go.mod",
            }:
                g.add_edge(
                    f"commit:{c.sha}",
                    file_id,
                    kind="COMMIT_CHANGED_DEPENDENCY",
                    change_type=fc.change_type,
                )

    # --- File -> function nodes -------------------------------------------
    for fa in file_analyses:
        file_id = f"file:{fa.path}"
        if file_id not in g:
            g.add_node(file_id, kind="file", path=fa.path)
        for fn in fa.functions:
            fn_id = f"function:{fa.path}::{fn.name}"
            g.add_node(
                fn_id,
                kind="function",
                name=fn.name,
                file=fa.path,
                lineno=fn.lineno,
                args=fn.args,
                guard_count=fn.guard_count,
            )
            g.add_edge(file_id, fn_id, kind="FILE_CONTAINS_FUNCTION")

    # --- Function -> test usage (heuristic: test files calling a function) -
    test_files = [fa for fa in file_analyses if _looks_like_test_file(fa.path)]
    non_test_functions = {
        fn.name: f"function:{fa.path}::{fn.name}"
        for fa in file_analyses if not _looks_like_test_file(fa.path)
        for fn in fa.functions
    }
    for tf in test_files:
        test_id = f"test:{tf.path}"
        g.add_node(test_id, kind="test", path=tf.path)
        called_names = {name for fn in tf.functions for name in fn.calls}
        for name in called_names:
            if name in non_test_functions:
                g.add_edge(non_test_functions[name], test_id, kind="FUNCTION_USED_BY_TEST")

    # --- Function -> function CALLS edges (real call sites, not just names) -
    # Built across every function in the repo (test and non-test) so Signal 9
    # in why_analysis can find *every* caller of a function whose signature
    # changed — including callers in files the commit itself never touched,
    # which is exactly the "nobody updated the call site" case it looks for.
    # Name-based, like FUNCTION_USED_BY_TEST above: two functions with the
    # same name in different files/classes are not distinguished. That's a
    # known heuristic limit (see README "known limitations"), not new here.
    all_functions_by_name: dict[str, list[str]] = {}
    for fa in file_analyses:
        for fn in fa.functions:
            all_functions_by_name.setdefault(fn.name, []).append(f"function:{fa.path}::{fn.name}")

    for fa in file_analyses:
        for fn in fa.functions:
            caller_id = f"function:{fa.path}::{fn.name}"
            for cs in fn.call_sites:
                for callee_id in all_functions_by_name.get(cs.name, []):
                    if callee_id == caller_id:
                        continue  # skip direct self-recursion
                    g.add_edge(
                        caller_id, callee_id,
                        kind="CALLS",
                        arg_count=cs.arg_count,
                        keyword_args=cs.keyword_args,
                        lineno=cs.lineno,
                    )

    # --- File -> file IMPORTS edges (local, in-repo imports only) ----------
    # External-package imports never match _resolve_local_imports and are
    # correctly dropped here — package-level dependencies are already
    # tracked separately via FILE_DEPENDS_ON_PACKAGE below.
    for importer_path, targets in _resolve_local_imports(file_analyses).items():
        importer_id = f"file:{importer_path}"
        if importer_id not in g:
            g.add_node(importer_id, kind="file", path=importer_path)
        for target_path in targets:
            target_id = f"file:{target_path}"
            if target_id not in g:
                g.add_node(target_id, kind="file", path=target_path)
            g.add_edge(importer_id, target_id, kind="IMPORTS")

    # --- Dependency nodes ---------------------------------------------------
    for dep_file in detect_dependency_files(repo_path):
        for dep in parse_dependency_file(repo_path, dep_file):
            dep_id = f"dependency:{dep['name']}"
            if dep_id not in g:
                g.add_node(dep_id, kind="dependency", name=dep["name"], version=dep.get("version", ""))
            manifest_id = f"file:{dep_file}"
            if manifest_id not in g:
                g.add_node(manifest_id, kind="file", path=dep_file)
            g.add_edge(manifest_id, dep_id, kind="FILE_DEPENDS_ON_PACKAGE")

    return g


def annotate_suspect_confidence(g: nx.MultiDiGraph, suspects: list) -> None:
    """
    NEW. Writes each Suspect's confidence/summary back onto its matching
    commit node, in place. Must be called after why_analysis.rank_suspects()
    and before graph_to_json() — otherwise the serialized graph has no way
    for the frontend to know which commits are suspects, forcing it to
    cross-reference the separate `suspects` list by sha on every render.

    Only commit nodes that made it into `suspects` (i.e. scored above
    min_confidence) get annotated; everything else is left as-is so the
    frontend can distinguish "not a suspect" from "suspect, low confidence".
    """
    by_sha = {s.commit_sha: s for s in suspects}
    for node, data in g.nodes(data=True):
        if data.get("kind") != "commit":
            continue
        s = by_sha.get(data.get("sha"))
        if s is None:
            continue
        data["suspect_confidence"] = s.confidence
        data["suspect_summary"] = s.likely_cause_summary


def _looks_like_test_file(path: str) -> bool:
    p = path.lower()
    return "test" in p.split("/")[-1] or p.split("/")[0] in {"tests", "test"}


def find_import_cycles(graph_json: dict) -> list[list[str]]:
    """
    Circular in-repo imports (file A imports B imports ... imports A),
    computed from the already-serialized graph JSON (graph_to_json's
    output) rather than a live nx.MultiDiGraph — this is called from the
    API layer, which only has the persisted JSON, not the in-memory graph
    build_evidence_graph produced. Genuine code smell independent of any
    specific commit; surfaced by the /code-graph endpoint as a top-level
    "cycles" field, not tied into WHY-analysis scoring, since a cycle is a
    standing property of the codebase's current shape, not evidence about
    which commit is at fault.

    Deduplicates rotations of the same cycle (A->B->C->A and B->C->A->B
    are the same cycle) by sorting each to a canonical starting point.
    """
    path_by_id = {n["id"]: n.get("path", n["id"]) for n in graph_json.get("nodes", []) if n.get("kind") == "file"}
    file_edges = [
        (e["source"], e["target"]) for e in graph_json.get("edges", [])
        if e.get("kind") == "IMPORTS" and e["source"] in path_by_id and e["target"] in path_by_id
    ]
    if not file_edges:
        return []
    file_graph = nx.DiGraph()
    file_graph.add_edges_from(file_edges)

    seen: set[tuple[str, ...]] = set()
    cycles: list[list[str]] = []
    for cycle in nx.simple_cycles(file_graph):
        if len(cycle) < 2:
            continue  # nx.simple_cycles can report self-loops as length-1; IMPORTS never self-loops (see build_evidence_graph), but guard anyway
        min_idx = cycle.index(min(cycle))
        canonical = tuple(cycle[min_idx:] + cycle[:min_idx])
        if canonical in seen:
            continue
        seen.add(canonical)
        cycles.append([path_by_id[n] for n in canonical])
    return cycles


def graph_to_json(g: nx.MultiDiGraph) -> dict:
    """Serialize the graph into a simple {nodes, edges} JSON shape for the API/frontend."""
    nodes = [{"id": n, **{k: v for k, v in data.items()}} for n, data in g.nodes(data=True)]
    edges = [
        {"source": u, "target": v, **{k: val for k, val in data.items()}}
        for u, v, data in g.edges(data=True)
    ]
    return {"nodes": nodes, "edges": edges}


def evidence_for_node(g: nx.MultiDiGraph, node_id: str) -> dict:
    """Return a node's data plus its immediate incoming/outgoing edges — used
    by the 'click a node to see evidence' UI feature. Operates on a live
    networkx graph — only usable where one is in memory (e.g. same request
    that just built it). See evidence_for_node_json for the persisted-graph
    equivalent used by the API."""
    if node_id not in g:
        return {"error": "node not found"}
    incoming = [
        {"from": u, "kind": data.get("kind"), **data}
        for u, _, data in g.in_edges(node_id, data=True)
    ]
    outgoing = [
        {"to": v, "kind": data.get("kind"), **data}
        for _, v, data in g.out_edges(node_id, data=True)
    ]
    return {"node": dict(g.nodes[node_id]), "incoming": incoming, "outgoing": outgoing}


def evidence_for_node_json(graph_json: dict, node_id: str) -> dict:
    """
    NEW. Same contract as evidence_for_node, but operates on the serialized
    {nodes, edges} dict produced by graph_to_json() and stored in
    Analysis.result() — this is what the API actually has on hand when
    serving GET /api/analysis/{id}/graph/node/{node_id}, since only the JSON
    form (not a live nx.MultiDiGraph) is persisted between the background
    job and the request that later asks about one node.

    Kept as a separate function rather than reconstructing an nx.MultiDiGraph
    from JSON on every call — that round-trip is pure overhead for what's
    just two list filters.
    """
    nodes_by_id = {n["id"]: n for n in graph_json.get("nodes", [])}
    if node_id not in nodes_by_id:
        return {"error": "node not found"}

    incoming = [
        {"from": e["source"], **{k: v for k, v in e.items() if k not in ("source", "target")}}
        for e in graph_json.get("edges", [])
        if e["target"] == node_id
    ]
    outgoing = [
        {"to": e["target"], **{k: v for k, v in e.items() if k not in ("source", "target")}}
        for e in graph_json.get("edges", [])
        if e["source"] == node_id
    ]
    node = {k: v for k, v in nodes_by_id[node_id].items() if k != "id"}
    return {"node": node, "incoming": incoming, "outgoing": outgoing}
