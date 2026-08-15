"""
Phase 9 (new) — Counterfactual debugging.

Everything else in AUTOPSY is static: WHY analysis (Phase 4) scores commits
from diffs and metadata alone, and regression_detection.py can only report
a *confirmed* regression today via real GitHub CI history (ci_status.py).
This module adds the second, self-contained way to get a confirmed answer:
actually run the test suite with a suspect commit removed, and check
whether the specific failing test(s) pass again.

This is the one place in AUTOPSY that executes arbitrary repository code
on purpose — twice over, in fact: once when installing the target repo's
own declared dependencies (setup.py/build backends can run arbitrary code
at install time, same class of risk as running the tests themselves), and
again when actually running its test suite. Treat every function here as
operating on untrusted input:
- Hard wall-clock timeout on every subprocess call, no exceptions.
- Dependencies are installed into a disposable venv (Python) or the
  worktree's own node_modules (JS) — NEVER into this service's own
  Python environment. Installing an arbitrary repo's dependencies into
  the same interpreter this FastAPI app runs in could silently break or
  crash the running service (version conflicts with fastapi/sqlalchemy/
  etc. sharing the same site-packages).
- Restricted environment (see _SUBPROCESS_ENV_ALLOWLIST) and resource
  ceilings on every untrusted subprocess call.
- Everything happens inside a disposable git worktree, never the
  original clone, and both the worktree and its venv are always removed,
  even on failure.

Because a suspect commit may itself change the dependency manifest (e.g.
a requirements.txt edit — exactly the kind of commit this feature exists
to verify), dependencies are installed TWICE: once for the baseline run
(commit present) and once more after the revert (commit removed) — the
two runs are not guaranteed to need the same package set.

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

# Wall-clock cap on a single test-suite invocation. Arbitrary cloned
# repos may have slow or hanging test suites — this is a safety valve,
# not a tuning knob for legitimate long-running suites.
DEFAULT_TEST_TIMEOUT_SECONDS = 120

# Wall-clock cap on a single dependency-install step. Heavy dependency
# trees (e.g. langchain + its transitive deps) can genuinely take
# minutes to resolve and download — this needs real headroom, unlike the
# test timeout above. A run_counterfactual call can install twice (once
# per side of the revert), so worst case is roughly
# 2 * (install + test) — this executes as a background job, not inside
# an HTTP request, so a several-minute total is acceptable.
DEFAULT_INSTALL_TIMEOUT_SECONDS = 300

# --- Process-level hardening ------------------------------------------
#
# This module runs arbitrary, untrusted code (both at dependency-install
# time and at test-run time). The current deployment (single container,
# no container-in-container capability) can't give each run its own
# sandboxed OS/VM boundary — that requires infrastructure this module
# can't provide from inside itself (a separate execution service, or a
# microVM provider like E2B/Modal/Daytona). Until that exists, these
# limits are the honest, achievable ceiling: they cap what a single
# runaway or malicious process can consume or reach *within the same
# container*, they do not isolate it from the container itself.

_TEST_SUBPROCESS_CPU_SECONDS = 90
_TEST_SUBPROCESS_MEMORY_BYTES = 1_500_000_000  # ~1.5 GB

# Installs get more headroom than test runs — resolving/building wheels
# for a heavy dependency tree can spike memory higher than actually
# running the resulting test suite does.
_INSTALL_SUBPROCESS_CPU_SECONDS = 240
_INSTALL_SUBPROCESS_MEMORY_BYTES = 3_000_000_000  # ~3 GB

# Environment variables passed to any untrusted subprocess (installs and
# test runs alike). Deliberately an allowlist, not the inherited
# os.environ — a cloned repo's install/test process must never be able
# to read this service's own secrets (DB URL, API keys, etc.) just
# because they happened to be in the parent process's environment.
_SUBPROCESS_ENV_ALLOWLIST = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}


def _sandboxed_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in _SUBPROCESS_ENV_ALLOWLIST}


def _limit_resources(cpu_seconds: int, memory_bytes: int):
    """Returns a preexec_fn closure applying the given ceilings — used so
    installs and test runs can have different limits."""
    def _apply():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    return _apply


class CounterfactualError(Exception):
    """Base class for all errors raised by this module."""


class UnsupportedTestFramework(CounterfactualError):
    pass


class WorktreeError(CounterfactualError):
    pass


class DependencyInstallError(CounterfactualError):
    """
    Raised when installing the target repo's own dependencies fails.
    Distinct from WorktreeError so callers/logs can tell "git operation
    failed" apart from "the repo's own package installation failed" —
    the latter is often the repo's fault (broken pin, private package
    index) rather than anything AUTOPSY did wrong.
    """


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
    test_timeout_seconds: int = DEFAULT_TEST_TIMEOUT_SECONDS,
    install_timeout_seconds: int = DEFAULT_INSTALL_TIMEOUT_SECONDS,
) -> CounterfactualResult:
    """
    Runs the test suite twice — once at HEAD, once at HEAD with commit_sha
    reverted — inside a disposable worktree, installing the target repo's
    own dependencies fresh before each run (see module docstring for why
    this happens twice), and compares which specific tests fail in each.

    `framework` must be "pytest" or "jest"; callers should get this from
    detect.detect_test_framework() and filter to supported ones before
    calling. Raises UnsupportedTestFramework otherwise, rather than
    silently no-op'ing — a caller storing "verified" results must never
    be able to mistake "we didn't know how to run this" for "we ran it
    and it passed".
    """
    if framework not in ("pytest", "jest"):
        raise UnsupportedTestFramework(
            f"No test runner configured for framework '{framework}'. Supported: pytest, jest"
        )

    short_sha = commit_sha[:8]
    worktree_path = Path(repo_dir).parent / f"autopsy-cf-{uuid.uuid4().hex[:10]}"

    try:
        _add_worktree(repo_dir, worktree_path, timeout_seconds=60)

        _install_dependencies(worktree_path, framework, install_timeout_seconds)
        baseline = _run_tests(worktree_path, framework, test_timeout_seconds)

        _revert_commit(worktree_path, commit_sha, timeout_seconds=60)

        # Re-install: the reverted commit may itself have changed the
        # dependency manifest (this is exactly the case that surfaced
        # this whole code path — a requirements.txt-editing commit).
        _install_dependencies(worktree_path, framework, install_timeout_seconds)
        without_commit = _run_tests(worktree_path, framework, test_timeout_seconds)

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
        # Suite failed (e.g. non-test exit code, collection error, or a
        # dependency-install failure surfaced as a fake "failure") but we
        # couldn't identify which specific tests failed — too coarse to
        # claim causation.
        return False
    remaining = set(without_commit.failing_tests)
    return any(t not in remaining for t in baseline.failing_tests)


def _run_trusted(cmd: list[str], cwd: Path, timeout_seconds: int) -> subprocess.CompletedProcess:
    """Used for git operations only — trusted commands, full inherited env, no resource caps."""
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout_seconds, check=False,
    )


def _run_untrusted(
    cmd: list[str], cwd: Path, timeout_seconds: int, cpu_seconds: int, memory_bytes: int
) -> subprocess.CompletedProcess:
    """
    Used for anything that executes code from the target repo itself:
    dependency installation (setup.py/build backends can run arbitrary
    code) and the test suite invocation. Restricted env and resource
    ceilings on top of the wall-clock timeout.

    Note: preexec_fn is not fully thread-safe in a multi-threaded process
    (this runs inside a FastAPI BackgroundTasks worker thread). For a
    single resource.setrlimit call with no locking, this is a widely used
    pattern and low-risk in practice — but if replay runs ever hang
    intermittently, this is the first place to look.
    """
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
        env=_sandboxed_env(),
        preexec_fn=_limit_resources(cpu_seconds, memory_bytes),
    )


def _add_worktree(repo_dir: Path, worktree_path: Path, timeout_seconds: int) -> None:
    try:
        result = _run_trusted(
            ["git", "worktree", "add", "--detach", str(worktree_path), "HEAD"],
            cwd=repo_dir, timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"git worktree add timed out: {exc}") from exc
    if result.returncode != 0:
        raise WorktreeError(f"git worktree add failed: {result.stderr.strip()[:500]}")


def _remove_worktree(repo_dir: Path, worktree_path: Path) -> None:
    """
    Always called from a `finally`. Best-effort on top of best-effort:
    `git worktree remove` first (keeps git's own bookkeeping clean), then
    an unconditional shutil.rmtree of whatever's left (which also cleans
    up the venv created inside the worktree, since it lives under the
    same directory) — a half-broken worktree must never accumulate on
    disk across analysis runs.
    """
    try:
        _run_trusted(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=repo_dir, timeout_seconds=30)
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
        result = _run_trusted(
            ["git", "revert", "--no-commit", "--no-edit", commit_sha],
            cwd=worktree_path, timeout_seconds=timeout_seconds,
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
            f"git revert of {commit_sha[:8]} failed (likely a conflict): {result.stderr.strip()[:500]}"
        )


# --- Dependency installation --------------------------------------------

def _venv_python(worktree_path: Path) -> Path:
    return worktree_path / ".autopsy_venv" / "bin" / "python"


def _install_dependencies(worktree_path: Path, framework: str, timeout_seconds: int) -> None:
    """
    Installs the TARGET REPO's own declared dependencies — not AUTOPSY's.
    Python deps go into a disposable venv created inside the worktree
    (never AUTOPSY's own interpreter — see module docstring for why).
    JS deps go into the worktree's own node_modules via npm, which is
    already isolated per-project by convention.

    Best-effort in the sense that a missing manifest isn't an error (some
    repos have no separate lockfile, or dependencies are inlined some
    other way) — but a manifest that's PRESENT and fails to install IS an
    error (DependencyInstallError), since a test run against an
    incompletely-installed environment produces meaningless results, not
    a legitimate pass/fail signal.
    """
    if framework == "pytest":
        _install_python_dependencies(worktree_path, timeout_seconds)
    elif framework == "jest":
        _install_node_dependencies(worktree_path, timeout_seconds)


def _install_python_dependencies(worktree_path: Path, timeout_seconds: int) -> None:
    venv_dir = worktree_path / ".autopsy_venv"
    if not venv_dir.exists():
        try:
            result = _run_trusted(["python3", "-m", "venv", str(venv_dir)], cwd=worktree_path, timeout_seconds=60)
        except subprocess.TimeoutExpired as exc:
            raise DependencyInstallError(f"venv creation timed out: {exc}") from exc
        if result.returncode != 0:
            raise DependencyInstallError(f"venv creation failed: {result.stderr.strip()[:500]}")

    python = str(_venv_python(worktree_path))
    requirements = worktree_path / "requirements.txt"
    pyproject = worktree_path / "pyproject.toml"

    try:
        if requirements.exists():
            result = _run_untrusted(
                [python, "-m", "pip", "install", "--no-input", "-r", "requirements.txt"],
                cwd=worktree_path, timeout_seconds=timeout_seconds,
                cpu_seconds=_INSTALL_SUBPROCESS_CPU_SECONDS, memory_bytes=_INSTALL_SUBPROCESS_MEMORY_BYTES,
            )
            if result.returncode != 0:
                raise DependencyInstallError(
                    f"pip install -r requirements.txt failed: {result.stderr.strip()[-1000:]}"
                )
        elif pyproject.exists():
            result = _run_untrusted(
                [python, "-m", "pip", "install", "--no-input", "."],
                cwd=worktree_path, timeout_seconds=timeout_seconds,
                cpu_seconds=_INSTALL_SUBPROCESS_CPU_SECONDS, memory_bytes=_INSTALL_SUBPROCESS_MEMORY_BYTES,
            )
            if result.returncode != 0:
                raise DependencyInstallError(f"pip install . failed: {result.stderr.strip()[-1000:]}")
    except subprocess.TimeoutExpired as exc:
        raise DependencyInstallError(f"dependency install timed out: {exc}") from exc

    # pytest itself is frequently a dev-only dependency NOT listed in a
    # project's runtime requirements.txt (detect.py finds pytest via test
    # files / config presence, not by parsing the manifest) — so it isn't
    # guaranteed to already be in the venv after the block above. Install
    # it explicitly, every time, rather than assuming.
    try:
        result = _run_untrusted(
            [python, "-m", "pip", "install", "--no-input", "pytest"],
            cwd=worktree_path, timeout_seconds=60,
            cpu_seconds=_INSTALL_SUBPROCESS_CPU_SECONDS, memory_bytes=_INSTALL_SUBPROCESS_MEMORY_BYTES,
        )
        if result.returncode != 0:
            raise DependencyInstallError(f"pip install pytest failed: {result.stderr.strip()[-500:]}")
    except subprocess.TimeoutExpired as exc:
        raise DependencyInstallError(f"pytest install timed out: {exc}") from exc


def _install_node_dependencies(worktree_path: Path, timeout_seconds: int) -> None:
    package_json = worktree_path / "package.json"
    if not package_json.exists():
        return
    try:
        result = _run_untrusted(
            ["npm", "install", "--no-audit", "--no-fund"],
            cwd=worktree_path, timeout_seconds=timeout_seconds,
            cpu_seconds=_INSTALL_SUBPROCESS_CPU_SECONDS, memory_bytes=_INSTALL_SUBPROCESS_MEMORY_BYTES,
        )
    except subprocess.TimeoutExpired as exc:
        raise DependencyInstallError(f"npm install timed out: {exc}") from exc
    if result.returncode != 0:
        raise DependencyInstallError(f"npm install failed: {result.stderr.strip()[-1000:]}")


# --- Test execution -------------------------------------------------------

def _test_command(worktree_path: Path, framework: str) -> list[str]:
    if framework == "pytest":
        # No --timeout flag: that requires the pytest-timeout plugin,
        # which isn't guaranteed to be installed (and wasn't, in
        # practice — this is what surfaced the whole missing-install-step
        # bug this module now fixes). The wall-clock timeout on the
        # subprocess call itself (see _run_untrusted's caller) is the
        # real safety net; no plugin dependency needed for that.
        return [str(_venv_python(worktree_path)), "-m", "pytest", "-q", "--tb=short"]
    if framework == "jest":
        return ["npx", "jest", "--ci", "--silent"]
    raise UnsupportedTestFramework(framework)


def _run_tests(worktree_path: Path, framework: str, timeout_seconds: int) -> TestRunResult:
    cmd = _test_command(worktree_path, framework)
    try:
        result = _run_untrusted(
            cmd, cwd=worktree_path, timeout_seconds=timeout_seconds,
            cpu_seconds=_TEST_SUBPROCESS_CPU_SECONDS, memory_bytes=_TEST_SUBPROCESS_MEMORY_BYTES,
        )
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return TestRunResult(passed=False, timed_out=True, raw_output=partial[:4000])
    except FileNotFoundError as exc:
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
