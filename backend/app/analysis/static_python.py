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
class CallSite:
    """One call expression found inside a function body."""
    name: str            # callee name (only simple `Name` calls, e.g. `foo(...)`)
    arg_count: int        # number of positional args at this call site
    keyword_args: list[str] = field(default_factory=list)
    lineno: int = 0


@dataclass
class FunctionInfo:
    name: str
    file: str
    lineno: int
    end_lineno: int
    args: list[str] = field(default_factory=list)         # parameter names, in signature order
    calls: list[str] = field(default_factory=list)         # flat callee names (back-compat)
    call_sites: list[CallSite] = field(default_factory=list)  # calls with arg-shape detail
    guard_count: int = 0   # number of `raise` / `assert` statements in the body — used to
                            # detect a function that quietly lost validation logic


@dataclass
class FileAnalysis:
    path: str
    functions: list[FunctionInfo] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    parse_error: str | None = None


def _function_args(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """
    Parameter names in call order: positional-only + regular positional-or-
    keyword args. Deliberately excludes *args/**kwargs and keyword-only args
    — those don't participate in the "caller passes N positional args"
    signature-break check this feeds (Signal 9).
    """
    a = node.args
    return [arg.arg for arg in list(a.posonlyargs) + list(a.args)]


def _count_guards(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """
    Count `raise` and `assert` statements in a function body (nested walk,
    excluding nested function/class defs so a guard in a helper doesn't get
    attributed to its enclosing function). Used by Signal 8 to detect a
    function that lost input-validation logic between commits — a bare
    line-count diff can't tell "removed a guard" from "removed a comment",
    but this can.
    """
    count = 0
    for n in ast.walk(node):
        if n is not node and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue  # don't descend into nested defs, walked separately
        if isinstance(n, (ast.Raise, ast.Assert)):
            count += 1
    return count


def _parse_source(source: str, rel_path: str) -> FileAnalysis:
    """
    Parse Python source text (already read — from disk or a git blob) into a
    FileAnalysis. Split out from analyze_python_file so callers that need to
    parse historical blob content (e.g. WHY-analysis signals comparing a
    file's state at a commit vs. its parent) don't need a file on disk.
    """
    analysis = FileAnalysis(path=rel_path)
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError as exc:
        analysis.parse_error = str(exc)
        return analysis

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            call_sites: list[CallSite] = []
            for n in ast.walk(node):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                    call_sites.append(CallSite(
                        name=n.func.id,
                        arg_count=len(n.args),
                        keyword_args=[kw.arg for kw in n.keywords if kw.arg],
                        lineno=getattr(n, "lineno", 0),
                    ))
            analysis.functions.append(
                FunctionInfo(
                    name=node.name,
                    file=rel_path,
                    lineno=node.lineno,
                    end_lineno=getattr(node, "end_lineno", node.lineno),
                    args=_function_args(node),
                    calls=[c.name for c in call_sites],
                    call_sites=call_sites,
                    guard_count=_count_guards(node),
                )
            )
        elif isinstance(node, ast.Import):
            analysis.imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                analysis.imports.append(node.module)

    return analysis


def analyze_python_source(source: str, rel_path: str) -> FileAnalysis:
    """Public entry point for parsing source text that isn't on disk (e.g. a
    git blob fetched via get_file_content_at_commit)."""
    return _parse_source(source, rel_path)


def analyze_python_file(repo_path: Path, rel_path: str) -> FileAnalysis:
    full_path = repo_path / rel_path
    try:
        source = full_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        analysis = FileAnalysis(path=rel_path)
        analysis.parse_error = str(exc)
        return analysis
    return _parse_source(source, rel_path)


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
