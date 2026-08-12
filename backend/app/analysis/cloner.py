"""
Phase 1 — Repository cloning.

Clones a validated public GitHub URL into an isolated temp directory with a
hard timeout and size enforcement. Never executes repository code.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from app.security import (
    CLONE_TIMEOUT_SECONDS,
    MAX_REPO_SIZE_BYTES,
    enforce_repo_size_limit,
    new_isolated_workdir,
    validate_github_url,
)


class CloneError(RuntimeError):
    pass


def clone_repository(raw_url: str, base_dir: str | None = None) -> Path:
    """
    Validate `raw_url`, clone it (shallow, single branch is NOT used because
    we need history — but depth is still capped) into an isolated temp dir,
    enforce the size limit, and return the local path.
    """
    url = validate_github_url(raw_url)
    job_dir = new_isolated_workdir(base_dir)
    dest = job_dir / "repo"

    # Use the git CLI via subprocess with a fixed argv list (no shell=True,
    # so there is no command-injection surface even though `url` is already
    # validated). --no-single-branch keeps history depth reasonable while
    # still giving us commit history to analyze.
    cmd = [
        "git",
        # Never fetch submodules: a malicious repo could point a submodule
        # at file:// or ext:: URLs to read the host filesystem or run
        # arbitrary commands. We only ever want top-level history/content.
        "-c", "protocol.file.allow=never",
        "-c", "protocol.ext.allow=never",
        "-c", "core.hooksPath=/dev/null",
        "clone",
        "--no-tags",
        url,
        str(dest),
    ]
    # GIT_TERMINAL_PROMPT=0 stops git from blocking on a username/password
    # prompt for private/nonexistent repos (it would otherwise hang until
    # CLONE_TIMEOUT_SECONDS instead of failing fast). GIT_ALLOW_PROTOCOL
    # pins the allowed transport so a redirect or crafted URL can't smuggle
    # in a different protocol.
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "http:https",
    }
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLONE_TIMEOUT_SECONDS,
            shell=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise CloneError(f"Clone timed out after {CLONE_TIMEOUT_SECONDS}s.") from exc

    if result.returncode != 0:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise CloneError(f"git clone failed: {result.stderr.strip()[:500]}")

    try:
        enforce_repo_size_limit(dest, MAX_REPO_SIZE_BYTES)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise

    return dest


def cleanup_workdir(path: Path) -> None:
    """Remove a job's temp directory (repo dir's parent = the job dir)."""
    job_dir = path.parent if path.name == "repo" else path
    shutil.rmtree(job_dir, ignore_errors=True)
