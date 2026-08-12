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
            COMMIT_AUTHORED_BY
"""
from __future__ import annotations

from pathlib import Path

import networkx as nx

from app.analysis.dependency_parser import parse_dependency_file
from app.analysis.detect import detect_dependency_files
from app.analysis.git_history import CommitRecord
from app.analysis.static_python import FileAnalysis


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


def _looks_like_test_file(path: str) -> bool:
    p = path.lower()
    return "test" in p.split("/")[-1] or p.split("/")[0] in {"tests", "test"}


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
    by the 'click a node to see evidence' UI feature."""
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
