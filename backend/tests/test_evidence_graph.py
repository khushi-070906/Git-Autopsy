from app.analysis import evidence_graph, git_history, static_python


def _build_graph(repo):
    commits = git_history.extract_history(repo)
    file_analyses = static_python.analyze_repository_python_files(repo)
    return evidence_graph.build_evidence_graph(repo, commits, file_analyses)


def test_graph_has_commit_and_file_nodes(demo_repo):
    g = _build_graph(demo_repo)
    kinds = {d["kind"] for _, d in g.nodes(data=True)}
    assert "commit" in kinds
    assert "file" in kinds
    assert "function" in kinds
    assert "dependency" in kinds


def test_commit_changed_dependency_edge_exists(demo_repo):
    g = _build_graph(demo_repo)
    dep_edges = [
        (u, v) for u, v, d in g.edges(data=True)
        if d.get("kind") == "COMMIT_CHANGED_DEPENDENCY"
    ]
    assert len(dep_edges) >= 1


def test_evidence_for_node_returns_incoming_and_outgoing(demo_repo):
    g = _build_graph(demo_repo)
    file_node = "file:requirements.txt"
    result = evidence_graph.evidence_for_node(g, file_node)
    assert "incoming" in result and "outgoing" in result
    assert len(result["incoming"]) >= 1


def test_graph_to_json_serializable(demo_repo):
    import json
    g = _build_graph(demo_repo)
    data = evidence_graph.graph_to_json(g)
    json.dumps(data)  # must not raise
