"""
Phase 5 — Dependency manifest parsing.

Parses manifest files into (name, version_spec) pairs. Never installs
anything; only reads and parses text.

Also provides per-commit manifest diffing (diff_dependency_manifest) so the
WHY analysis can know exactly which package(s) changed in a given commit,
classify dev-only vs. runtime, and — new in this round — classify a
version *change* by severity (major/minor/patch) so a breaking-looking
major bump can be weighted higher than a routine patch bump.
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

DEV_ONLY_TOOLS = {
    "prettier", "eslint", "black", "flake8", "mypy", "ruff", "pytest",
    "pytest-cov", "isort", "pylint", "husky", "lint-staged", "nodemon",
    "typescript", "@types/node", "jest", "vitest", "commitlint",
}


def _is_dev_dependency(dep: dict) -> bool:
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
    execution).

    Returns {"added": [...], "removed": [...], "changed": [...]} where each
    entry is a dependency dict as produced by the relevant parser. Each
    "changed" entry additionally carries "old_version" so callers can
    classify bump severity via classify_version_bump().
    """
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
    changed = []
    for n in (curr.keys() & prev.keys()):
        if curr[n].get("version") != prev[n].get("version"):
            entry = dict(curr[n])
            entry["old_version"] = prev[n].get("version", "")
            changed.append(entry)

    return {"added": added, "removed": removed, "changed": changed}


def _clean_version_parts(v: str) -> list[int] | None:
    """
    Strip common version-spec prefixes (^, ~, >=, ==, etc.) and pull out up
    to 3 leading numeric components. Returns None if nothing numeric could
    be extracted (e.g. a git-ref version, an empty string, "latest").
    """
    if not v:
        return None
    v = v.strip()
    v = re.sub(r"^[~^=<>! ]+", "", v)
    parts = re.split(r"[.\-+]", v)
    nums: list[int] = []
    for p in parts[:3]:
        m = re.match(r"^(\d+)", p)
        if not m:
            break
        nums.append(int(m.group(1)))
    return nums or None


def classify_version_bump(old_version: str, new_version: str) -> str:
    """
    Classify a version change as "major", "minor", "patch", "same", or
    "unknown" (when either version string can't be parsed as numeric
    semver-like components — e.g. a git URL, a branch name, "latest").

    Used to weight the dependency-change signal: a major-version bump on a
    runtime dependency is a much stronger regression risk than a patch
    bump, so they shouldn't score identically.
    """
    old_nums = _clean_version_parts(old_version)
    new_nums = _clean_version_parts(new_version)
    if old_nums is None or new_nums is None:
        return "unknown"
    old_nums = old_nums + [0] * (3 - len(old_nums))
    new_nums = new_nums + [0] * (3 - len(new_nums))
    if old_nums[0] != new_nums[0]:
        return "major"
    if old_nums[1] != new_nums[1]:
        return "minor"
    if old_nums[2] != new_nums[2]:
        return "patch"
    return "same"
