"""
Phase 9 (new) — Counterfactual debugging.

Everything else in AUTOPSY is static: WHY analysis (Phase 4) scores commits
from diffs and metadata alone, and regression_detection.py can only report
a *confirmed* regression today via real GitHub CI history (ci_status.py).
This module adds the second, self-contained way to get a confirmed answer:
actually run the test suite with a suspect commit removed, and check
whether the specific failing test(s) pass again.

This is the one place in AUTOPSY that executes arbitrary repository code
on purpose. Treat every function here as operating on untrusted input:
- Hard wall-clock timeout on every subprocess call, no exceptions.
- No network access assumed available (isolate at the container level —
  this module doesn't and can't enforce that from inside itself).
- Everything happens inside a disposable git worktree, never the original
  clone, and the worktree is always removed, even on failure.

This module deliberately does NOT decide *whether* to run — that's a
product/ops decision (on-demand, triggered by a user action) made by the
caller. This module only knows how to run one replay safely and report
a structured result.
"""
from __future__ import annotations

import logging
import os
import resource
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("autopsy.counterfactual")

# Wall-clock cap on any single subprocess call made by this module (git
# operations and test-suite runs alike). Arbitrary cloned repos may have
# slow or hanging test suites — this is a safety valve, not a tuning knob
# for legitimate long-running suites. Override via run_counterfactual's
# timeout_seconds param for a specific call if a repo genuinely needs more,
# but the default must stay conservative.
DEFAULT_TIMEOUT_SECONDS = 120

# --- Process-level hardening ------------------------------------------
#
# This module runs arbitrary, untrusted test code. The current deployment
# (single container, no container-in-container capability) can't give
# each run its own sandboxed OS/VM boundary — that requires infrastructure
# this module can't provide from inside itself (a separate execution
# service, or a microVM provider like E2B/Modal/Daytona). Until that
# exists, these limits are the honest, achievable ceiling: they cap what
# a single runaway or malicious test suite can consume or reach *within
# the same container*, they do not isolate it from the container itself.

# CPU-seconds and address-space (bytes) ceiling applied to the test
# subprocess via resource.setrlimit, on top of the wall-clock timeout
# already enforced by subprocess.run's `timeout`. A CPU-heavy but
# non-hanging process (e.g. a tight infinite loop that still yields I/O)
# can burn CPU without necessarily tripping a wall-clock timeout in every
# case — RLIMIT_CPU is the backstop for that.
_TEST_SUBPROCESS_CPU_SECONDS = 90
_TEST_SUBPROCESS_MEMORY_BYTES = 1_500_000_000  # ~1.5 GB

# Environment variables passed to a replay subprocess. Deliberately an
# allowlist, not the inherited os.environ — a cloned repo's test suite
# must never be able to read this service's own secrets (DB URL, API
# keys, etc.) just because they happened to be in the parent process's
# environment. PATH is required for the test runner binary to resolve at
# all; HOME avoids tools that assume it's set (pip caches, etc.) failing
# oddly.
_SUBPROCESS_ENV_ALLOWLIST = {"PATH", "HOME", "LANG", "LC_ALL"}


def _sandboxed_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in _SUBPROCESS_ENV_ALLOWLIST}


def _limit_test_subprocess_resources() -> None:
    """
    Passed as `preexec_fn` to subprocess.run for test-suite invocations
    only (not git operations, which are trusted). Runs in the forked
    child before exec — sets hard resource ceilings that apply to that
    process (and, notably, do NOT get inherited more permissively by
    anything it execs).
    """
    resource.setrlimit(
        resource.RLIMIT_CPU, (_TEST_SUBPROCESS_CPU_SECONDS, _TEST_SUBPROCESS_CPU_SECONDS)
    )
    resource.setrlimit(
        resource.RLIMIT_AS, (_TEST_SUBPROCESS_MEMORY_BYTES, _TEST_SUBPROCESS_MEMORY_BYTES)
    )


# Maps a detected test framework (as returned by detect.detect_test_framework)
# to the command used to run it and a parser for pulling failing test IDs
# out of its output. Extend this as detect.py learns new frameworks —
# a framework with no entry here simply can't be counterfactually verified
# (run_counterfactual raises UnsupportedTestFramework) rather than guessing
# a command that might do something unexpected.
_FRAMEWORK_COMMANDS: dict[str, list[str]] = {
    "pytest": ["pytest", "--timeout=60", "-q", "--tb=no"],
    "jest": ["npx", "jest", "--ci", "--silent"],
}


class CounterfactualError(Exception):
    """Base class for all errors raised by this module."""


class UnsupportedTestFramework(CounterfactualError):
    pass


class WorktreeError(CounterfactualError):
    pass


@dataclass
class TestRunResult:
    passed: bool
    failing_tests: list[str] = field(default_factory=list)
    timed_out: bool = False
    raw_output: str = ""  # truncated; see _run_tests


@dataclass
class CounterfactualResult:
    commit_sha: str
    short_sha: str
    framework: str
    baseline: TestRunResult   # HEAD, commit present
    without_commit: TestRunResult  # HEAD with commit_sha reverted
    removes_failure: bool  # True only if a specific failing test in
                            # baseline is absent from without_commit's
                            # failing set — not just "exit code changed"
    error: str | None = None


def run_counterfactual(
    repo_dir: Path,
    commit_sha: str,
    framework: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> CounterfactualResult:
    """
    Runs the test suite twice — once at HEAD, once at HEAD with commit_sha
    reverted — inside a disposable worktree, and compares which specific
    tests fail in each run.

    `framework` must be one of _FRAMEWORK_COMMANDS's keys; callers should
    get this from detect.detect_test_framework() and filter to supported
    ones before calling. Raises UnsupportedTestFramework otherwise, rather
    than silently no-op'ing — a caller storing "verified" results must
    never be able to mistake "we didn't know how to run this" for
    "we ran it and it passed".
    """
    if framework not in _FRAMEWORK_COMMANDS:
        raise UnsupportedTestFramework(
            f"No test command configured for framework '{framework}'. "
            f"Supported: {sorted(_FRAMEWORK_COMMANDS)}"
        )

    short_sha = commit_sha[:8]
    worktree_path = Path(repo_dir).parent / f"autopsy-cf-{uuid.uuid4().hex[:10]}"

    try:
        _add_worktree(repo_dir, worktree_path, timeout_seconds)

        baseline = _run_tests(worktree_path, framework, timeout_seconds)

        _revert_commit(worktree_path, commit_sha, timeout_seconds)
        without_commit = _run_tests(worktree_path, framework, timeout_seconds)

        removes_failure = _failure_removed(baseline, without_commit)

        return CounterfactualResult(
            commit_sha=commit_sha,
            short_sha=short_sha,
            framework=framework,
            baseline=baseline,
            without_commit=without_commit,
            removes_failure=removes_failure,
        )

    except CounterfactualError as exc:
        logger.warning("Counterfactual run failed for %s: %s", short_sha, exc)
        empty = TestRunResult(passed=False)
        return CounterfactualResult(
            commit_sha=commit_sha,
            short_sha=short_sha,
            framework=framework,
            baseline=empty,
            without_commit=empty,
            removes_failure=False,
            error=str(exc),
        )
    finally:
        _remove_worktree(repo_dir, worktree_path)


def _failure_removed(baseline: TestRunResult, without_commit: TestRunResult) -> bool:
    """
    True only when baseline actually failed (nothing to "remove" if it
    didn't), the counterfactual run itself completed cleanly (a timeout on
    either side makes the comparison meaningless, not a pass), and at
    least one specific failing test id present in baseline is absent from
    without_commit's failing set. Deliberately NOT just
    "baseline.passed is False and without_commit.passed is True" — an
    unrelated flaky test newly failing (or an unrelated one newly passing)
    must not be read as "commit_sha caused the regression".
    """
    if baseline.passed:
        return False
    if baseline.timed_out or without_commit.timed_out:
        return False
    if not baseline.failing_tests:
        # Suite failed (e.g. non-test exit code) but we couldn't identify
        # which specific tests failed — too coarse to claim causation.
        return False
    remaining = set(without_commit.failing_tests)
    return any(t not in remaining for t in baseline.failing_tests)


def _run(cmd: list[str], cwd: Path, timeout_seconds: int) -> subprocess.CompletedProcess:
    """Used for git operations only — trusted commands, full inherited env."""
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )


def _run_untrusted(cmd: list[str], cwd: Path, timeout_seconds: int) -> subprocess.CompletedProcess:
    """
    Used for the actual test-suite invocation only — the one command in
    this module that runs code from the target repo itself (test files
    can, and often do, execute arbitrary imports/fixtures). Restricted
    env (see _SUBPROCESS_ENV_ALLOWLIST) and CPU/memory ceilings (see
    _limit_test_subprocess_resources) on top of the same wall-clock
    timeout _run already gets from subprocess.run.
    """
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
        env=_sandboxed_env(),
        preexec_fn=_limit_test_subprocess_resources,
    )


def _add_worktree(repo_dir: Path, worktree_path: Path, timeout_seconds: int) -> None:
    try:
        result = _run(
            ["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"],
            cwd=repo_dir,
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"git worktree add timed out: {exc}") from exc
    if result.returncode != 0:
        raise WorktreeError(f"git worktree add failed: {result.stderr.strip()[:500]}")


def _remove_worktree(repo_dir: Path, worktree_path: Path) -> None:
    """
    Always called from a `finally`. Best-effort on top of best-effort:
    `git worktree remove` first (keeps git's own bookkeeping clean), then
    an unconditional shutil.rmtree of whatever's left, since a half-broken
    worktree (e.g. the test run crashed mid-write) must never be allowed
    to accumulate on disk across analysis runs.
    """
    try:
        _run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=repo_dir, timeout_seconds=30)
    except Exception:
        logger.warning("git worktree remove failed for %s; falling back to rmtree", worktree_path, exc_info=True)
    shutil.rmtree(worktree_path, ignore_errors=True)


def _revert_commit(worktree_path: Path, commit_sha: str, timeout_seconds: int) -> None:
    """
    Reverts commit_sha on top of the worktree's current HEAD, without
    committing — chosen over rebuilding history via cherry-pick-all-except,
    since revert is a single, well-understood operation that fails loudly
    (non-zero exit, conflict markers) on conflict rather than silently
    producing a subtly wrong tree.
    """
    try:
        result = _run(
            ["git", "revert", "--no-commit", "--no-edit", commit_sha],
            cwd=worktree_path,
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"git revert timed out: {exc}") from exc
    if result.returncode != 0:
        # Most commonly a conflict — the commit's changes were later
        # modified/removed by other commits, so a clean revert isn't
        # possible. That's a legitimate "can't verify this one", not a
        # crash — surfaced as a CounterfactualError so the caller can
        # report "inconclusive" rather than a false result.
        raise WorktreeError(
            f"git revert of {commit_sha[:8]} failed (likely a conflict): "
            f"{result.stderr.strip()[:500]}"
        )


def _run_tests(worktree_path: Path, framework: str, timeout_seconds: int) -> TestRunResult:
    cmd = _FRAMEWORK_COMMANDS[framework]
    try:
        result = _run_untrusted(cmd, cwd=worktree_path, timeout_seconds=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return TestRunResult(passed=False, timed_out=True, raw_output=partial[:4000])
    except FileNotFoundError as exc:
        # Test runner binary not present in this environment/container —
        # not the same as "tests ran and failed".
        raise WorktreeError(f"test command not found: {exc}") from exc

    output = (result.stdout or "") + "\n" + (result.stderr or "")
    failing_tests = _parse_failing_tests(framework, output)
    return TestRunResult(
        passed=(result.returncode == 0),
        failing_tests=failing_tests,
        raw_output=output[:4000],
    )


def _parse_failing_tests(framework: str, output: str) -> list[str]:
    """
    Extracts individual failing test identifiers from raw test-runner
    output, so _failure_removed can compare specific tests rather than
    just pass/fail counts. Deliberately conservative — a parser that
    fails to recognize a framework's format returns [] rather than
    guessing, which makes _failure_removed correctly refuse to claim
    causation (see its docstring) instead of matching on noise.
    """
    failing: list[str] = []
    if framework == "pytest":
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("FAILED "):
                # "FAILED tests/test_foo.py::test_bar - AssertionError: ..."
                failing.append(line[len("FAILED "):].split(" - ")[0].strip())
    elif framework == "jest":
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("✕") or line.startswith("×"):
                failing.append(line.lstrip("✕× ").strip())
    return failing
