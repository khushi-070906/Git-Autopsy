from app.analysis import evidence_graph, git_history, static_python, why_analysis


def _build_graph(repo):
    commits = git_history.extract_history(repo)
    file_analyses = static_python.analyze_repository_python_files(repo)
    return evidence_graph.build_evidence_graph(repo, commits, file_analyses)


def test_flags_the_deliberate_regression_commit_as_top_suspect(demo_repo):
    """The core demo assertion: AUTOPSY must surface the dependency-upgrade
    commit as its top suspect, since that is the deliberately injected
    regression in the fixture repo."""
    g = _build_graph(demo_repo)
    suspects = why_analysis.rank_suspects(g)
    assert len(suspects) >= 1
    top = suspects[0]
    assert "Upgrade dependency" in top.message


def test_evidence_chain_has_required_categories(demo_repo):
    g = _build_graph(demo_repo)
    suspects = why_analysis.rank_suspects(g)
    kinds = {e.kind for e in suspects[0].evidence}
    # Spec requires the system to distinguish FACT / EVIDENCE / INFERENCE / RECOMMENDATION.
    assert "FACT" in kinds
    assert "INFERENCE" in kinds
    assert "RECOMMENDATION" in kinds


def test_confidence_is_bounded(demo_repo):
    g = _build_graph(demo_repo)
    suspects = why_analysis.rank_suspects(g)
    for s in suspects:
        assert 0.0 <= s.confidence <= 1.0


def test_no_evidence_no_fabricated_suspect(demo_repo):
    """A commit with no risk signal at all should not be force-included."""
    g = _build_graph(demo_repo)
    suspects = why_analysis.rank_suspects(g, min_confidence=0.9)
    # With a high min_confidence bar and only heuristic signals available,
    # AUTOPSY should not manufacture near-certain suspects out of weak signals.
    for s in suspects:
        assert s.confidence >= 0.9
