"""
Phase 2 (extension) — Lightweight static analysis for JavaScript/TypeScript.

Python repos get real AST-based analysis via static_python.py. JS/TS repos
previously got nothing at all (file_analyses = [] whenever the dominant
language wasn't Python), which meant Signal 4 in why_analysis.py
("changed code touches N function(s) referenced by tests") never fired for
the majority of real-world repos.

This is NOT a real parser — no external JS/TS parsing dependency is added.
It's a regex-based best-effort extractor: good enough to catch common
function/arrow-function declaration patterns and populate the same
FileAnalysis/FunctionInfo shape that static_python.py produces, so
evidence_graph.py can build FILE_CONTAINS_FUNCTION / FUNCTION_USED_BY_TEST
edges for JS/TS exactly the same way it does for Python. It will miss some
patterns (destructured params, class methods written unusually, complex
arrow chains) — that's an acceptable false-negative rate in exchange for
zero new dependencies and no risk of executing repository code.

Never executes repository code. Only reads source text and applies regexes.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.analysis.static_python import FileAnalysis, FunctionInfo

# Matches, in order of how commonly they appear in real JS/TS code:
#   function foo(...)                      / export function foo(...)
#   export default function foo(...)
#   const foo = (...) => ...                / export const foo = (...) => ...
#   const foo = async (...) => ...
#   const foo = someArg => ...              (single unparenthesized param)
FUNCTION_PATTERNS = [
    re.compile(r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("),
    re.compile(r"(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\("),
    re.compile(r"(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?[A-Za-z_$][\w$]*\s*=>"),
]

# Crude call-site extraction: `name(` anywhere in a small window after a
# function's definition. Over-inclusive by design (will pick up keywords
# like `if`/`for` that happen to be followed by a paren in some styles is
# avoided by the pattern itself requiring an identifier start) — false
# positives here just mean FUNCTION_USED_BY_TEST edges that are slightly
# too generous, which is a safe direction to err in for a heuristic signal.
CALL_PATTERN = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")

# Common JS/TS keywords that syntactically look like calls (`if (`, `for (`,
# `while (`, `switch (`, `catch (`, `function (`) but aren't function
# invocations — excluded so they don't pollute the "calls" list used to
# build FUNCTION_USED_BY_TEST edges.
_NOT_CALLS = {"if", "for", "while", "switch", "catch", "function", "return"}

_EXTENSIONS = (".js", ".jsx", ".ts", ".tsx")


def analyze_js_file(repo_path: Path, rel_path: str) -> FileAnalysis:
    full_path = repo_path / rel_path
    analysis = FileAnalysis(path=rel_path)
    try:
        source = full_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        analysis.parse_error = str(exc)
        return analysis

    lines = source.splitlines()
    seen_names: set[str] = set()

    for i, line in enumerate(lines):
        for pattern in FUNCTION_PATTERNS:
            m = pattern.search(line)
            if not m:
                continue
            name = m.group(1)
            if name in seen_names:
                continue
            seen_names.add(name)

            # Look at a small window of following lines for call-sites —
            # cheap stand-in for "what does this function call", enough to
            # link a source function to a test that calls it by name.
            window = "\n".join(lines[i:i + 30])
            calls = [
                c for c in dict.fromkeys(CALL_PATTERN.findall(window))
                if c not in _NOT_CALLS and c != name
            ]
            analysis.functions.append(
                FunctionInfo(name=name, file=rel_path, lineno=i + 1, end_lineno=i + 1, calls=calls)
            )

    return analysis


def analyze_repository_js_files(repo_path: Path, max_files: int = 300) -> list[FileAnalysis]:
    """
    Mirrors static_python.analyze_repository_python_files: walks the repo,
    skips .git and node_modules (the JS equivalent of skipping
    site-packages), caps at max_files for the same reason (bound analysis
    time on very large repos).
    """
    results = []
    files = [
        p for p in repo_path.rglob("*")
        if p.is_file()
        and p.suffix in _EXTENSIONS
        and ".git" not in p.parts
        and "node_modules" not in p.parts
    ][:max_files]
    for f in files:
        rel = str(f.relative_to(repo_path))
        results.append(analyze_js_file(repo_path, rel))
    return results
