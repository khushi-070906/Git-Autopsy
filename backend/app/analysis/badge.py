"""
Repo-level health badge, shields.io "endpoint badge" compatible.

Lets anyone embed a live AUTOPSY health score in their own README:

    [![AUTOPSY health](https://img.shields.io/endpoint?url=https://YOUR_HOST/api/badge/{owner}/{repo}.json)](https://YOUR_HOST/report/{owner}/{repo})

shields.io fetches the URL behind `?url=`, expects the JSON schema
documented at https://shields.io/badges/endpoint-badge, and renders the
badge itself — this module only needs to serve that JSON.

Design constraints (important — read before changing this file):

  - READ-ONLY, DB-ONLY. This route must NEVER trigger cloning or a new
    analysis run. It only reports the most recent COMPLETED analysis
    already on file for that repo. Badges get embedded in public READMEs
    and re-fetched by shields.io's own cache on a schedule outside our
    control — if this endpoint could kick off work, it would be a trivial
    way for anyone to DoS the analysis pipeline just by adding a badge to
    a popular README. If no analysis exists yet, the badge says so and
    links to the site to request one; it does not do the work itself.

  - Fast. Shields.io imposes its own timeout on endpoint badges and will
    render a broken/error badge if we're slow. This should be a single
    indexed lookup, nothing else.

  - Cheap on error. Any lookup failure (repo never analyzed, DB hiccup,
    bad owner/repo string) must degrade to a valid "unknown" badge, never
    a 500 — a broken image in someone's README is bad marketing.

Relies on Analysis.repo_url (already a plain, non-indexed String column
as of the current schema) and Analysis.updated_at to find "most recent
completed analysis for this repo" — NOT Analysis.id, since id is a UUID
string and string-sorting UUIDs is not chronological order. If repo_url
lookups get slow at scale, add an index:

    repo_url = Column(String, index=True, nullable=False)
"""
from __future__ import annotations

from fastapi import APIRouter, Response
from sqlalchemy import desc

from app.database import Analysis, SessionLocal

router = APIRouter(prefix="/api/badge", tags=["badge"])

# shields.io endpoint-badge color names. Anything not in this map falls
# back to "lightgrey".
_RISK_COLORS = {
    "LOW": "brightgreen",
    "MEDIUM": "yellow",
    "HIGH": "red",
}

_CACHE_CONTROL = "public, max-age=3600"  # 1h: badge doesn't need to be real-time


def _normalize_repo_url(owner: str, repo: str) -> str:
    """
    Rebuilds the canonical https://github.com/<owner>/<repo> form used
    everywhere else in AUTOPSY (see ci_status._parse_owner_repo), so a
    badge for /api/badge/foo/bar matches however repo_url was stored
    when the analysis was created.
    """
    owner = owner.strip().strip("/")
    repo = repo.strip().strip("/").removesuffix(".git")
    return f"https://github.com/{owner}/{repo}"


def _shield_payload(label: str, message: str, color: str) -> dict:
    # https://shields.io/badges/endpoint-badge — schemaVersion is required
    # and must be exactly 1.
    return {"schemaVersion": 1, "label": label, "message": message, "color": color}


def _badge_response(payload: dict) -> Response:
    import json

    return Response(
        content=json.dumps(payload),
        media_type="application/json",
        headers={
            "Cache-Control": _CACHE_CONTROL,
            # shields.io fetches server-side, but some people hot-link
            # this JSON directly from browser-side tooling too.
            "Access-Control-Allow-Origin": "*",
        },
    )


def _latest_completed_analysis(repo_url: str) -> Analysis | None:
    db = SessionLocal()
    try:
        return (
            db.query(Analysis)
            .filter(Analysis.repo_url == repo_url, Analysis.status == "completed")
            .order_by(desc(Analysis.updated_at))
            .first()
        )
    finally:
        db.close()


@router.get("/{owner}/{repo}.json")
def health_badge(owner: str, repo: str) -> Response:
    """
    Shields.io endpoint-badge JSON for the given repo's most recent
    completed AUTOPSY analysis. Never raises — any problem degrades to a
    valid "unknown"/"not analyzed" badge rather than an error response,
    since this is meant to render cleanly inside someone else's README.
    """
    try:
        repo_url = _normalize_repo_url(owner, repo)
        analysis = _latest_completed_analysis(repo_url)
    except Exception:
        return _badge_response(_shield_payload("AUTOPSY", "error", "lightgrey"))

    if analysis is None:
        return _badge_response(_shield_payload("AUTOPSY", "not analyzed", "lightgrey"))

    result = analysis.result() or {}
    health = result.get("health") or {}
    score = health.get("repository_health_score")
    risk_level = health.get("risk_level")

    if score is None or risk_level is None:
        return _badge_response(_shield_payload("AUTOPSY", "unknown", "lightgrey"))

    color = _RISK_COLORS.get(risk_level, "lightgrey")
    return _badge_response(_shield_payload("AUTOPSY health", f"{score}/100", color))


@router.get("/{owner}/{repo}/markdown")
def health_badge_markdown(owner: str, repo: str, host: str = "") -> dict:
    """
    Convenience endpoint that returns ready-to-paste README markdown, so
    users don't have to hand-construct the shields.io URL themselves.
    `host` should be the deployed AUTOPSY origin, e.g.
    "https://autopsy.example.com" — passed as a query param since this
    module doesn't know its own public URL.
    """
    owner = owner.strip().strip("/")
    repo = repo.strip().strip("/").removesuffix(".git")
    base = host.rstrip("/") if host else ""
    badge_url = f"{base}/api/badge/{owner}/{repo}.json"
    report_url = f"{base}/report/{owner}/{repo}"
    markdown = f"[![AUTOPSY health](https://img.shields.io/endpoint?url={badge_url})]({report_url})"
    return {"markdown": markdown}
