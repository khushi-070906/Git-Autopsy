#!/usr/bin/env python3
"""
AUTOPSY demo runner.

Builds a small local git repository with a deliberately injected regression
(a dependency upgrade that silently changes tokenizer behavior), then runs
the full deterministic analysis pipeline against it and prints a case
report — no network access or GitHub account required.

Usage:
    python run_demo.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "backend"))

from app.analysis import detect, evidence_graph, git_history, regression_detection, static_python, why_analysis  # noqa: E402


def _run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def build_demo_repo(root: Path) -> Path:
    repo = root / "demo"
    repo.mkdir()
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "dev@example.com"], repo)
    _run(["git", "config", "user.name", "Dev"], repo)

    (repo / "model_loader.py").write_text(
        "def tokenize(text):\n    return text.split(' ')\n\n"
        "def load_model(path):\n    return {'path': path, 'tokenizer': tokenize}\n"
    )
    (repo / "requirements.txt").write_text("requests==2.28.0\n")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_model_loader.py").write_text(
        "from model_loader import tokenize\n\n"
        "def test_tokenize_basic():\n    assert tokenize('hello world') == ['hello', 'world']\n"
    )
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "Initial working version with basic tokenizer"], repo)

    (repo / "model_loader.py").write_text(
        "def tokenize(text):\n    return text.split(' ')\n\n"
        "def load_model(path, cache=True):\n    return {'path': path, 'tokenizer': tokenize, 'cache': cache}\n"
    )
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "Add caching option to load_model"], repo)

    (repo / "requirements.txt").write_text("requests==2.31.0\nfast-tokenizer==2.0.0\n")
    (repo / "model_loader.py").write_text(
        "from fast_tokenizer import tokenize as _tokenize\n\n"
        "def tokenize(text):\n    # switched to new tokenizer library after dependency upgrade\n"
        "    return _tokenize(text)\n\n"
        "def load_model(path, cache=True):\n    return {'path': path, 'tokenizer': tokenize, 'cache': cache}\n"
    )
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "Upgrade dependency versions and switch to fast-tokenizer"], repo)
    return repo


def main() -> None:
    start = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="autopsy_demo_"))
    try:
        print("=" * 64)
        print("AUTOPSY — Forensic analysis for software")
        print("=" * 64)
        print("\n[1/5] Building demo repository with an injected regression...")
        repo = build_demo_repo(tmp)

        print("[2/5] Extracting git history...")
        commits = git_history.extract_history(repo)

        print("[3/5] Running static analysis...")
        lang = detect.detect_language(repo)
        file_analyses = static_python.analyze_repository_python_files(repo)

        print("[4/5] Building the Evidence Graph...")
        g = evidence_graph.build_evidence_graph(repo, commits, file_analyses)

        print("[5/5] Running WHY analysis...\n")
        suspects = why_analysis.rank_suspects(g)
        health = regression_detection.compute_repository_health(g, suspects)

        print("-" * 64)
        print("OVERVIEW")
        print("-" * 64)
        print(f"  Repository Health   {health['repository_health_score']} / 100")
        print(f"  Risk Level          {health['risk_level']}")
        print(f"  Likely Regressions  {health['likely_regressions']}")
        print(f"  Dependency Risks    {health['dependency_risks']}")
        print(f"  Suspicious Changes  {health['suspicious_changes']}")

        if suspects:
            top = suspects[0]
            print("\n" + "-" * 64)
            print("LIKELY ROOT CAUSE")
            print("-" * 64)
            print(f"  Commit:      {top.short_sha} — {top.message}")
            print(f"  Confidence:  {round(top.confidence * 100)}%")
            print(f"  Summary:     {top.likely_cause_summary}")
            print(f"  Files:       {', '.join(top.affected_files)}")
            print("\n  Evidence chain:")
            for e in top.evidence:
                print(f"    [{e.kind:<14}] {e.text}")

        elapsed = time.time() - start
        print("\n" + "=" * 64)
        print(f"Demo completed in {elapsed:.1f}s")
        print("=" * 64)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
