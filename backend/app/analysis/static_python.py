"""
Phase 2 — Safe static analysis of Python source using the `ast` module.

Never executes repository code. Only parses source text into an AST and
walks it read-only to extract function/class definitions and imports.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FunctionInfo:
    name: str
    file: str
    lineno: int
    end_lineno: int
    calls: list[str] = field(default_factory=list)


@dataclass
class FileAnalysis:
    path: str
    functions: list[FunctionInfo] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    parse_error: str | None = None


def analyze_python_file(repo_path: Path, rel_path: str) -> FileAnalysis:
    full_path = repo_path / rel_path
    analysis = FileAnalysis(path=rel_path)
    try:
        source = full_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=rel_path)
    except (SyntaxError, OSError, UnicodeDecodeError) as exc:
        analysis.parse_error = str(exc)
        return analysis

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls = [
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            ]
            analysis.functions.append(
                FunctionInfo(
                    name=node.name,
                    file=rel_path,
                    lineno=node.lineno,
                    end_lineno=getattr(node, "end_lineno", node.lineno),
                    calls=calls,
                )
            )
        elif isinstance(node, ast.Import):
            analysis.imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                analysis.imports.append(node.module)

    return analysis


def analyze_repository_python_files(repo_path: Path, max_files: int = 300) -> list[FileAnalysis]:
    results = []
    py_files = [
        p for p in repo_path.rglob("*.py")
        if ".git" not in p.parts and "site-packages" not in p.parts
    ][:max_files]
    for f in py_files:
        rel = str(f.relative_to(repo_path))
        results.append(analyze_python_file(repo_path, rel))
    return results
