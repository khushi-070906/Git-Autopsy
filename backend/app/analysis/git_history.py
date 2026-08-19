"""
Phase 1/2 — Git history and diff extraction.

Turns raw git log/diff data into structured, serializable records that the
rest of the system (evidence graph, WHY analysis) can consume. No repository
code is ever executed here — only `git log` / `git diff` metadata is read.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import git


@dataclass
class FileChange:
    path: str
    change_type: str  # "A" added, "M" modified, "D" deleted, "R" renamed
    insertions: int
    deletions: int


@dataclass
class CommitRecord:
    sha: str
    short_sha: str
    author: str
    author_email: str
    date: str
    message: str
    files_changed: list[FileChange] = field(default_factory=list)
    parents: list[str] = field(default_factory=list)


def extract_history(repo_path: Path, max_commits: int = 500) -> list[CommitRecord]:
    """
    Walk commit history from HEAD, newest first, capped at `max_commits`
    to bound analysis time on very active repos.
    """
    repo = git.Repo(str(repo_path))
    records: list[CommitRecord] = []

    for commit in repo.iter_commits(max_count=max_commits):
        file_changes: list[FileChange] = []
        parent = commit.parents[0] if commit.parents else None

        try:
            diffs = parent.diff(commit) if parent else commit.diff(git.NULL_TREE)
        except Exception:
            diffs = []

        for d in diffs:
            path = d.b_path or d.a_path or "unknown"
            change_type = d.change_type or "M"
            insertions = deletions = 0
            try:
                stat_key = d.b_path or d.a_path
                stats = commit.stats.files.get(stat_key)
                if stats:
                    insertions = stats.get("insertions", 0)
                    deletions = stats.get("deletions", 0)
            except Exception:
                pass
            file_changes.append(
                FileChange(
                    path=path,
                    change_type=change_type,
                    insertions=insertions,
                    deletions=deletions,
                )
            )

        records.append(
            CommitRecord(
                sha=commit.hexsha,
                short_sha=commit.hexsha[:7],
                author=commit.author.name or "unknown",
                author_email=commit.author.email or "",
                date=commit.committed_datetime.isoformat(),
                message=commit.message.strip(),
                files_changed=file_changes,
                parents=[p.hexsha for p in commit.parents],
            )
        )

    return records


def get_commit_diff(repo_path: Path, sha: str, max_chars: int = 20000) -> str:
    """
    Return the unified diff text for a single commit against its first
    parent (or against the empty tree, for a repo's very first commit).
    Read-only — uses GitPython's Diffable.diff(create_patch=True), which
    never executes repository code. Truncated to max_chars so one huge
    generated-file commit can't blow up a response payload.
    """
    repo = git.Repo(str(repo_path))
    commit = repo.commit(sha)
    parent = commit.parents[0] if commit.parents else None
    try:
        # NULL_TREE is a GitPython sentinel understood by the Diffable API
        # (parent.diff / commit.diff) — it is NOT a valid arg to the raw
        # `git diff` CLI via repo.git.diff(), which is why this goes
        # through commit.diff() rather than a subprocess/CLI call.
        diffs = (parent.diff(commit, create_patch=True) if parent is not None
                 else commit.diff(git.NULL_TREE, create_patch=True))
        parts = []
        for d in diffs:
            if d.diff:
                parts.append(d.diff.decode("utf-8", errors="replace") if isinstance(d.diff, bytes) else d.diff)
        diff_text = "\n".join(parts)
    except Exception as exc:  # noqa: BLE001
        return f"(unable to generate diff: {exc})"

    if len(diff_text) > max_chars:
        omitted = len(diff_text) - max_chars
        diff_text = diff_text[:max_chars] + f"\n... (truncated, {omitted} more characters)"
    return diff_text


def get_file_content_at_commit(repo_path: Path, sha: str, file_path: str) -> Optional[str]:
    """Read a file's content as it existed at a specific commit, without checkout."""
    repo = git.Repo(str(repo_path))
    try:
        commit = repo.commit(sha)
        blob = commit.tree / file_path
        return blob.data_stream.read().decode("utf-8", errors="replace")
    except Exception:
        return None
