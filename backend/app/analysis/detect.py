"""
Phase 2 — Language, dependency-manifest, and test-framework detection.
"""
from __future__ import annotations

from pathlib import Path

LANGUAGE_EXTENSIONS = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".rb": "Ruby",
}

DEPENDENCY_FILES = [
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "poetry.lock",
    "Cargo.toml",
    "go.mod",
]

TEST_MARKERS = {
    "pytest": ["pytest.ini", "conftest.py", "pyproject.toml"],
    "jest": ["jest.config.js", "jest.config.ts"],
    "mocha": [".mocharc.json", ".mocharc.yml"],
    "go test": ["go.mod"],
    "cargo test": ["Cargo.toml"],
}


def detect_language(repo_path: Path) -> dict:
    """Count source files by extension; report the dominant language."""
    counts: dict[str, int] = {}
    for f in repo_path.rglob("*"):
        if f.is_file() and ".git" not in f.parts:
            lang = LANGUAGE_EXTENSIONS.get(f.suffix)
            if lang:
                counts[lang] = counts.get(lang, 0) + 1
    dominant = max(counts, key=counts.get) if counts else "Unknown"
    return {"dominant_language": dominant, "file_counts_by_language": counts}


def detect_dependency_files(repo_path: Path) -> list[str]:
    found = []
    for fname in DEPENDENCY_FILES:
        if (repo_path / fname).exists():
            found.append(fname)
    return found


def detect_test_framework(repo_path: Path) -> list[str]:
    found = []
    for framework, markers in TEST_MARKERS.items():
        for marker in markers:
            if (repo_path / marker).exists():
                found.append(framework)
                break
    # Also look for test directories as a weaker signal
    for d in ["tests", "test", "__tests__", "spec"]:
        if (repo_path / d).is_dir() and not found:
            found.append("unknown (test directory present)")
            break
    return sorted(set(found))
