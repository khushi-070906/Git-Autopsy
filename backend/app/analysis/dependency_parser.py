"""
Phase 5 — Dependency manifest parsing.

Parses manifest files into (name, version_spec) pairs. Never installs
anything; only reads and parses text.
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


def parse_dependency_file(repo_path: Path, filename: str) -> list[dict]:
    parser = PARSERS.get(filename)
    if not parser:
        return []
    fpath = repo_path / filename
    if not fpath.exists():
        return []
    text = fpath.read_text(encoding="utf-8", errors="replace")
    return parser(text)
