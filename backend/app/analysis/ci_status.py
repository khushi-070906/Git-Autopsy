"""
Phase 7 (extension) — Real CI status via GitHub's public Checks API.

Everything in why_analysis.py / regression_detection.py up to this point is
a STATIC heuristic — no repository code is ever executed, and
`has_test_execution_data` has always been hardcoded False. This module is
the one piece that can turn "statistical inference" into "confirmed
regression" for repos that actually run CI: it reads already-computed
pass/fail results from GitHub Actions (or any CI integrated with GitHub's
Checks API) via the public REST API. It does NOT run any code itself —
only reads status that GitHub's own CI already produced.

Design constraints:
  - Public, unauthenticated GitHub API access only (60 req/hour limit).
    No token handling, no secrets — this only works for public repos,
    same as the rest of AUTOPSY.
  - Must degrade gracefully. A repo with no CI configured, a rate-limited
    request, a network failure, or an unexpected API shape must all result
    in "no CI data available" — never an exception that aborts the whole
    analysis pipeline. Real CI data is a bonus signal, not a requirement.
  - Bounded: only checks the commits actually surfaced as suspects (not
    the full history) to stay well within the unauthenticated rate limit
    on a single analysis run.
  - Zero third-party dependencies. Uses only Python's stdlib (urllib).
    A previous version imported `requests` at module level, which meant
    that if `requests` wasn't installed in the deploy environment, the
    top-level `from app.analysis import (..., ci_status, ...)` in
    pipeline.py would raise ModuleNotFoundError at *import time* — before
    any of this module's careful error handling ever ran — and crash the
    whole app on boot rather than just degrading one analysis job. Using
    only the stdlib removes that failure mode entirely.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

GITHUB_API_BASE = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 8

# Conclusions GitHub's Checks API can report per check-run. Mapped down to
# a simple pass/fail/unknown so callers don't need to know GitHub's exact
# vocabulary.
_FAILURE_CONCLUSIONS = {"failure", "timed_out", "action_required", "startup_failure"}
_SUCCESS_CONCLUSIONS = {"success"}
_INCONCLUSIVE_CONCLUSIONS = {"neutral", "cancelled", "skipped", "stale"}


class CIStatusUnavailable(Exception):
    """Raised internally, always caught — never propagates out of this module."""


def _parse_owner_repo(repo_url: str) -> tuple[str, str] | None:
    """
    Extract (owner, repo) from a validated https://github.com/<owner>/<repo>
    URL. Returns None if the shape is unexpected rather than raising —
    this module never raises out to callers.
    """
    m = re.match(r"^https://github\.com/([^/]+)/([^/.]+)", repo_url.rstrip("/"))
    if not m:
        return None
    return m.group(1), m.group(2)


def _get_json(url: str) -> tuple[int, dict | None]:
    """
    GET a URL and return (status_code, parsed_json_or_None). Every failure
    mode — network error, timeout, non-200, bad JSON — is caught here and
    turned into a plain (status_code, None) or (0, None) so callers never
    need their own try/except around this.
    """
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "autopsy-ci-status",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            status = resp.getcode()
            body = resp.read()
    except urllib.error.HTTPError as e:
        # 404 (no checks for this commit), 403 (rate limited), 422, etc. —
        # still a well-formed HTTP response, just not a 2xx.
        return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError):
        # DNS failure, connection refused, timeout, etc.
        return 0, None

    if status != 200:
        return status, None

    try:
        return status, json.loads(body)
    except (ValueError, TypeError):
        return status, None


def fetch_commit_ci_status(repo_url: str, sha: str) -> str:
    """
    Returns one of: "passed", "failed", "inconclusive", "unknown".

    "unknown" covers every failure mode uniformly — no CI configured, repo
    not found, rate limited, network error, unexpected response shape.
    Callers should treat "unknown" exactly like having no CI data at all;
    this function is designed so a caller never needs a try/except of its
    own.
    """
    parsed = _parse_owner_repo(repo_url)
    if parsed is None:
        return "unknown"
    owner, repo = parsed

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/commits/{sha}/check-runs"
    _status, data = _get_json(url)
    if data is None:
        return "unknown"

    check_runs = data.get("check_runs", [])
    if not check_runs:
        return "unknown"

    conclusions = [c.get("conclusion") for c in check_runs if c.get("status") == "completed"]
    if not conclusions:
        return "unknown"  # still running, or no completed runs

    if any(c in _FAILURE_CONCLUSIONS for c in conclusions):
        return "failed"
    if all(c in _SUCCESS_CONCLUSIONS for c in conclusions):
        return "passed"
    if all(c in _SUCCESS_CONCLUSIONS or c in _INCONCLUSIVE_CONCLUSIONS for c in conclusions):
        return "passed"
    return "inconclusive"


def annotate_suspects_with_ci(repo_url: str, suspects: list, max_lookups: int = 10) -> dict:
    """
    Given the already-ranked suspects (highest confidence first), look up
    real CI status for at most `max_lookups` of them — bounded to stay
    well within the unauthenticated GitHub API rate limit on a single
    analysis run.

    Returns a dict: {commit_sha: "passed"|"failed"|"inconclusive"|"unknown"}
    for whichever commits were actually looked up. Never raises — any
    per-commit lookup failure just yields "unknown" for that commit and
    the loop continues.
    """
    results: dict[str, str] = {}
    for suspect in suspects[:max_lookups]:
        sha = getattr(suspect, "commit_sha", None) or suspect.get("commit_sha")
        if not sha:
            continue
        try:
            results[sha] = fetch_commit_ci_status(repo_url, sha)
        except Exception:
            # Belt-and-suspenders: fetch_commit_ci_status already catches
            # everything internally, but a bug here should never be able
            # to take down the whole analysis pipeline.
            results[sha] = "unknown"
    return results


def find_confirmed_regression(
    repo_url: str, commits: list, ci_status_by_sha: dict
) -> dict | None:
    """
    Given commits (newest-first, as produced by git_history.extract_history)
    and a sha -> status map from annotate_suspects_with_ci, look for a
    genuine last-passing -> first-failing transition among the commits we
    actually have CI data for.

    This is intentionally conservative: it only looks at the subset of
    commits with known status (results of annotate_suspects_with_ci, which
    is itself bounded to top suspects) — it does NOT walk full history,
    since that would need far more API calls than the unauthenticated rate
    limit allows for a single analysis. Returns None if no clean
    passed->failed adjacency is found in that subset; callers should treat
    that as "no confirmed regression found from CI data" and fall back to
    the heuristic top suspect, not as an error.
    """
    known = [c for c in commits if ci_status_by_sha.get(c.sha) in ("passed", "failed")]
    if len(known) < 2:
        return None

    # commits are newest-first; walk oldest-to-newest to find the first
    # passed -> failed transition
    known_oldest_first = list(reversed(known))
    for i in range(1, len(known_oldest_first)):
        prev_status = ci_status_by_sha.get(known_oldest_first[i - 1].sha)
        curr_status = ci_status_by_sha.get(known_oldest_first[i].sha)
        if prev_status == "passed" and curr_status == "failed":
            failing = known_oldest_first[i]
            return {
                "commit_sha": failing.sha,
                "short_sha": failing.short_sha,
                "message": failing.message.splitlines()[0][:200] if failing.message else "",
                "last_passing_sha": known_oldest_first[i - 1].sha,
                "last_passing_short_sha": known_oldest_first[i - 1].short_sha,
            }
    return None
