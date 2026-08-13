"""
Phase 5 — Dependency manifest parsing.

Parses manifest files into (name, version_spec) pairs. Never installs
anything; only reads and parses text.

Also provides per-commit manifest diffing (Fix, see below) so the WHY
analysis can know exactly which package(s) changed in a given commit,
rather than just "this commit touched a file named package.json" —
and can tell a dev-only tool (prettier, eslint, ...) apart from a
runtime dependency (torch, fastapi, ...) that can actually cause a
behavioral regression.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None


def parse_requirements_txt(text: str) -> list[dict]:
    deps = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        match = re.match(r"^([A-Za-z0-9_.\-\[\]]+)\s*([<>=!~].*)?$", line)
        if match:
            deps.append({"name": match.group(1), "version": (match.group(2) or "").strip()})
    return deps


def parse_package_json(text: str) -> list[dict]:
    deps = []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return deps
    for section in ("dependencies", "devDependencies"):
        for name, version in data.get(section, {}).items():
            deps.append({"name": name, "version": version, "dev": section == "devDependencies"})
    return deps


def parse_pyproject_toml(text: str) -> list[dict]:
    deps = []
    if tomllib is None:
        return deps
    try:
        data = tomllib.loads(text)
    except Exception:
        return deps
    # PEP 621
    for dep in data.get("project", {}).get("dependencies", []):
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*([<>=!~].*)?$", dep)
        if m:
            deps.append({"name": m.group(1), "version": (m.group(2) or "").strip()})
    # Poetry
    poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    for name, version in poetry_deps.items():
        if name.lower() == "python":
            continue
        deps.append({"name": name, "version": str(version)})
    return deps


def parse_go_mod(text: str) -> list[dict]:
    deps = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^([a-zA-Z0-9./_-]+)\s+(v[\d.]+\S*)", line)
        if m and not line.startswith(("module", "go ")):
            deps.append({"name": m.group(1), "version": m.group(2)})
    return deps


def parse_cargo_toml(text: str) -> list[dict]:
    deps = []
    if tomllib is None:
        return deps
    try:
        data = tomllib.loads(text)
    except Exception:
        return deps
    for name, version in data.get("dependencies", {}).items():
        v = version if isinstance(version, str) else version.get("version", "")
        deps.append({"name": name, "version": str(v)})
    return deps


PARSERS = {
    "requirements.txt": parse_requirements_txt,
    "package.json": parse_package_json,
    "pyproject.toml": parse_pyproject_toml,
    "go.mod": parse_go_mod,
    "Cargo.toml": parse_cargo_toml,
}

# Packages that are dev/build-tooling only and essentially never cause a
# runtime behavioral regression on their own. Used to downweight the
# "dependency changed" signal in why_analysis when only tooling like this
# moved. This list is intentionally conservative — false negatives here
# (a real runtime package we fail to recognize) just fall back to full
# weight, which is the safe default.
DEV_ONLY_TOOLS = {
    "prettier", "eslint", "black", "flake8", "mypy", "ruff", "pytest",
    "pytest-cov", "isort", "pylint", "husky", "lint-staged", "nodemon",
    "typescript", "@types/node", "jest", "vitest", "commitlint",
}


def _is_dev_dependency(dep: dict) -> bool:
    """
    True if a parsed dependency dict is dev/build-tooling only.

    package.json deps carry an explicit "dev" flag from parse_package_json.
    requirements.txt has no such concept, so we fall back to a name
    allowlist of well-known dev-only tools; anything not recognized is
    treated as a runtime dependency (safe default — don't under-weight
    something we don't recognize).
    """
    if dep.get("dev") is True:
        return True
    return dep.get("name", "").lower() in DEV_ONLY_TOOLS


def parse_dependency_file(repo_path: Path, filename: str) -> list[dict]:
    parser = PARSERS.get(filename)
    if not parser:
        return []
    fpath = repo_path / filename
    if not fpath.exists():
        return []
    text = fpath.read_text(encoding="utf-8", errors="replace")
    return parser(text)


def diff_dependency_manifest(
    repo_path: Path,
    sha: str,
    parent_sha: str | None,
    manifest_path: str,
) -> dict:
    """
    Return which dependencies were added / removed / changed in `manifest_path`
    between `parent_sha` and `sha`, using git blob reads (no checkout, no
    execution). This is what lets the WHY analysis know it was specifically
    `prettier` (a dev tool) vs. `torch` (a runtime dependency) that changed
    in a given commit, instead of just "package.json was touched".

    Returns {"added": [...], "removed": [...], "changed": [...]} where each
    entry is a dependency dict as produced by the relevant parser.
    """
    # Local import to avoid a circular import at module load time
    # (git_history doesn't depend on dependency_parser, but keeping this
    # import local mirrors the existing style used for the optional
    # AI layer's `import anthropic`).
    from app.analysis.git_history import get_file_content_at_commit

    filename = manifest_path.rsplit("/", 1)[-1]
    parser = PARSERS.get(filename)
    if not parser:
        return {"added": [], "removed": [], "changed": []}

    curr_text = get_file_content_at_commit(repo_path, sha, manifest_path) or ""
    prev_text = (
        get_file_content_at_commit(repo_path, parent_sha, manifest_path)
        if parent_sha else ""
    ) or ""

    curr = {d["name"]: d for d in parser(curr_text)}
    prev = {d["name"]: d for d in parser(prev_text)} if prev_text else {}

    added = [curr[n] for n in (curr.keys() - prev.keys())]
    removed = [prev[n] for n in (prev.keys() - curr.keys())]
    changed = [
        curr[n] for n in (curr.keys() & prev.keys())
        if curr[n].get("version") != prev[n].get("version")
    ]
    return {"added": added, "removed": removed, "changed": changed}
