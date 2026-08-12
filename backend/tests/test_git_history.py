from app.analysis import git_history


def test_extract_history_returns_all_commits(demo_repo):
    commits = git_history.extract_history(demo_repo)
    assert len(commits) == 3
    messages = [c.message for c in commits]
    assert any("Upgrade dependency" in m for m in messages)


def test_commits_have_file_changes(demo_repo):
    commits = git_history.extract_history(demo_repo)
    latest = commits[0]  # newest first
    changed_paths = [fc.path for fc in latest.files_changed]
    assert "requirements.txt" in changed_paths
    assert "model_loader.py" in changed_paths


def test_get_file_content_at_commit(demo_repo):
    commits = git_history.extract_history(demo_repo)
    initial_commit = commits[-1]
    content = git_history.get_file_content_at_commit(demo_repo, initial_commit.sha, "requirements.txt")
    assert "requests==2.28.0" in content
