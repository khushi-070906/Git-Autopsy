"""
Security utilities for AUTOPSY.

Every rule here exists because analyzing arbitrary public repositories is an
attacker-controlled input surface: repo names, branch names, file paths,
commit messages, and file contents are all untrusted.
"""
from __future__ import annotations

import os
import re
import tempfile
import uuid
from pathlib import Path

# Only allow https://github.com/<owner>/<repo>[.git] — nothing else.
# Owner/repo segments: alnum, dash, underscore, dot. No slashes, no '..'.
GITHUB_URL_RE = re.compile(
    r"^https://github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]{1,100})/"
    r"(?P<repo>[A-Za-z0-9_.-]{1,100})"
    r"(?:\.git)?/?$"
)

MAX_REPO_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB hard cap
CLONE_TIMEOUT_SECONDS = 120
ANALYSIS_TIMEOUT_SECONDS = 300


class InvalidRepositoryURL(ValueError):
    pass


class RepositoryTooLarge(RuntimeError):
    pass


def validate_github_url(raw_url: str) -> str:
    """
    Validate and normalize a GitHub repository URL.

    Rejects anything that isn't a plain https://github.com/<owner>/<repo>
    URL. This blocks:
      - command injection via shell metacharacters (;, |, &, `, $())
      - path traversal (..)
      - non-GitHub hosts (SSRF to internal services)
      - git protocol smuggling (file://, ext::, etc.)
    """
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise InvalidRepositoryURL("URL must be a non-empty string.")

    url = raw_url.strip()

    # Reject control/shell-dangerous characters outright before regex,
    # so a clever encoding can't slip something through.
    if any(ch in url for ch in [";", "|", "&", "`", "$", "\n", "\r", "\0", " "]):
        raise InvalidRepositoryURL("URL contains disallowed characters.")

    match = GITHUB_URL_RE.match(url)
    if not match:
        raise InvalidRepositoryURL(
            "Only URLs of the form https://github.com/<owner>/<repo> are allowed."
        )

    owner, repo = match.group("owner"), match.group("repo")

    if ".." in owner or ".." in repo:
        raise InvalidRepositoryURL("Path traversal sequence detected.")

    repo = repo[: -len(".git")] if repo.endswith(".git") else repo

    return f"https://github.com/{owner}/{repo}.git"


def new_isolated_workdir(base_dir: str | None = None) -> Path:
    """
    Create a fresh, isolated temp directory for a single analysis job.
    Never reuses directories; never derives paths from user input.
    """
    root = Path(base_dir) if base_dir else Path(tempfile.gettempdir()) / "autopsy_jobs"
    root.mkdir(parents=True, exist_ok=True)
    job_dir = root / f"job_{uuid.uuid4().hex}"
    job_dir.mkdir(parents=True, exist_ok=False)
    return job_dir


def safe_join(base: Path, *parts: str) -> Path:
    """
    Join path components under `base`, rejecting any result that escapes it.
    Use this any time a path is built from repository-controlled data
    (file names from git history, diff paths, etc.).
    """
    base = base.resolve()
    candidate = (base / Path(*parts)).resolve()
    if base not in candidate.parents and candidate != base:
        raise ValueError(f"Path traversal attempt blocked: {parts}")
    return candidate


def enforce_repo_size_limit(path: Path, max_bytes: int = MAX_REPO_SIZE_BYTES) -> int:
    """Walk a cloned repo and raise if it exceeds the configured size cap."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            try:
                total += os.path.getsize(fpath)
            except OSError:
                continue
            if total > max_bytes:
                raise RepositoryTooLarge(
                    f"Repository exceeds size limit of {max_bytes} bytes."
                )
    return total
