#!/usr/bin/env python3
"""
AUTOPSY evaluation harness.

Builds a battery of small synthetic git repositories, each with a
deliberately injected regression at a KNOWN commit, then runs the fully
deterministic analysis engine (git_history -> static_python -> evidence_graph
-> why_analysis) against every one of them and checks whether the engine's
top-ranked suspect actually matches the known-guilty commit.

This never calls the optional AI explanation layer, so it needs no
ANTHROPIC_API_KEY and no network access — it's a pure measurement of the
deterministic scoring model in app/analysis/why_analysis.py.

Usage:
    python evaluate.py                # run + print + write JSON + PNG chart
    python evaluate.py --no-chart     # skip matplotlib, JSON only
    python evaluate.py --trials 5     # repeat each scenario N times (fresh
                                       # tmp repo each time) to show variance
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.analysis import (  # noqa: E402
    detect,
    evidence_graph,
    git_history,
    regression_detection,
    static_python,
    why_analysis,
)

OUT_DIR = Path(__file__).parent / "eval_results"


def _run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def _git_init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "dev@example.com"], repo)
    _run(["git", "config", "user.name", "Dev"], repo)


def _commit(repo: Path, message: str) -> str:
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", message], repo)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    return sha


@dataclass
class Scenario:
    name: str
    description: str
    build: "callable"  # (repo: Path) -> guilty_sha: str


# --------------------------------------------------------------------------
# Scenarios. Each builds a handful of commits ending with one commit that
# injects a real regression, and returns that commit's sha as ground truth.
# --------------------------------------------------------------------------

def scenario_silent_dependency_bump(repo: Path) -> str:
    (repo / "model_loader.py").write_text(
        "def tokenize(text):\n    return text.split(' ')\n\n"
        "def load_model(path):\n    return {'path': path, 'tokenizer': tokenize}\n"
    )
    (repo / "requirements.txt").write_text("requests==2.28.0\n")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_model_loader.py").write_text(
        "from model_loader import tokenize\n\n"
        "def test_tokenize_basic():\n    assert tokenize('hello world') == ['hello', 'world']\n"
    )
    _commit(repo, "Initial working version with basic tokenizer")

    (repo / "model_loader.py").write_text(
        "def tokenize(text):\n    return text.split(' ')\n\n"
        "def load_model(path, cache=True):\n    return {'path': path, 'tokenizer': tokenize, 'cache': cache}\n"
    )
    _commit(repo, "Add caching option to load_model")

    # The injected regression: a dependency bump that silently swaps the
    # tokenizer behavior underneath callers who never touched this file.
    (repo / "requirements.txt").write_text("requests==2.31.0\nfast-tokenizer==2.0.0\n")
    (repo / "model_loader.py").write_text(
        "from fast_tokenizer import tokenize\n\n"
        "def load_model(path, cache=True):\n    return {'path': path, 'tokenizer': tokenize, 'cache': cache}\n"
    )
    guilty = _commit(repo, "Bump requests, adopt fast-tokenizer for perf")

    (repo / "README.md").write_text("# demo\n")
    _commit(repo, "Add readme")
    return guilty


def scenario_removed_null_check(repo: Path) -> str:
    (repo / "billing.py").write_text(
        "def charge(customer, amount):\n"
        "    if customer is None:\n"
        "        raise ValueError('no customer')\n"
        "    if amount <= 0:\n"
        "        raise ValueError('bad amount')\n"
        "    return {'customer': customer, 'amount': amount, 'status': 'charged'}\n"
    )
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_billing.py").write_text(
        "from billing import charge\n\n"
        "def test_charge_ok():\n    assert charge('acme', 10)['status'] == 'charged'\n"
    )
    _commit(repo, "Add billing.charge with input validation")

    (repo / "billing.py").write_text(
        "def charge(customer, amount):\n"
        "    if amount <= 0:\n"
        "        raise ValueError('bad amount')\n"
        "    return {'customer': customer, 'amount': amount, 'status': 'charged'}\n"
    )
    guilty = _commit(repo, "Refactor charge() for readability")

    (repo / "invoicing.py").write_text("def format_invoice(c):\n    return str(c)\n")
    _commit(repo, "Add invoice formatting helper")
    return guilty


def scenario_signature_change_breaks_caller(repo: Path) -> str:
    (repo / "geo.py").write_text(
        "def distance(lat1, lon1, lat2, lon2):\n"
        "    return ((lat1 - lat2) ** 2 + (lon1 - lon2) ** 2) ** 0.5\n"
    )
    (repo / "router.py").write_text(
        "from geo import distance\n\n"
        "def nearest(origin, points):\n"
        "    return min(points, key=lambda p: distance(origin[0], origin[1], p[0], p[1]))\n"
    )
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_router.py").write_text(
        "from router import nearest\n\n"
        "def test_nearest():\n    assert nearest((0, 0), [(1, 1), (5, 5)]) == (1, 1)\n"
    )
    _commit(repo, "Add distance + nearest routing helpers")

    # Regression: geo.distance's argument order silently changes, router.py
    # (the actual bug site callers hit) is never updated to match.
    (repo / "geo.py").write_text(
        "def distance(lon1, lat1, lon2, lat2):\n"
        "    return ((lat1 - lat2) ** 2 + (lon1 - lon2) ** 2) ** 0.5\n"
    )
    guilty = _commit(repo, "Switch geo.distance to lon-first argument order (GIS convention)")
    return guilty


SCENARIOS: list[Scenario] = [
    Scenario(
        "silent_dependency_bump",
        "A dependency swap silently changes tokenizer behavior underneath unrelated callers.",
        scenario_silent_dependency_bump,
    ),
    Scenario(
        "removed_null_check",
        "A 'readability' refactor quietly drops an input-validation branch.",
        scenario_removed_null_check,
    ),
    Scenario(
        "signature_change_breaks_caller",
        "A function's argument order changes but a caller is never updated.",
        scenario_signature_change_breaks_caller,
    ),
]


def run_pipeline(repo: Path):
    commits = git_history.extract_history(repo)
    language_info = detect.detect_language(repo)
    file_analyses = (
        static_python.analyze_repository_python_files(repo)
        if language_info["dominant_language"] == "Python"
        else []
    )
    g = evidence_graph.build_evidence_graph(repo, commits, file_analyses)
    suspects = why_analysis.rank_suspects(g)
    return suspects


def evaluate(trials: int = 1) -> dict:
    per_scenario = []
    all_correct_conf = []
    all_incorrect_conf = []
    top1_hits = 0
    top3_hits = 0
    total = 0

    for scenario in SCENARIOS:
        scenario_top1 = 0
        scenario_top3 = 0
        scenario_runs = []
        for _ in range(trials):
            tmp = Path(tempfile.mkdtemp(prefix="autopsy_eval_"))
            try:
                repo = tmp / "repo"
                _git_init(repo)
                guilty_sha = scenario.build(repo)
                suspects = run_pipeline(repo)

                ranked_shas = [s.commit_sha for s in suspects]
                top1 = bool(ranked_shas) and ranked_shas[0] == guilty_sha
                top3 = guilty_sha in ranked_shas[:3]

                if suspects:
                    top_conf = suspects[0].confidence
                    if top1:
                        all_correct_conf.append(top_conf)
                    else:
                        all_incorrect_conf.append(top_conf)

                scenario_top1 += int(top1)
                scenario_top3 += int(top3)
                total += 1
                top1_hits += int(top1)
                top3_hits += int(top3)
                scenario_runs.append({
                    "top1_correct": top1,
                    "top3_correct": top3,
                    "top_confidence": suspects[0].confidence if suspects else None,
                    "predicted_sha": ranked_shas[0][:7] if ranked_shas else None,
                    "guilty_sha": guilty_sha[:7],
                })
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        per_scenario.append({
            "name": scenario.name,
            "description": scenario.description,
            "top1_accuracy": scenario_top1 / trials,
            "top3_accuracy": scenario_top3 / trials,
            "trials": trials,
            "runs": scenario_runs,
        })

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "engine": "deterministic (no API key / no network used)",
        "total_trials": total,
        "top1_accuracy": top1_hits / total if total else 0.0,
        "top3_accuracy": top3_hits / total if total else 0.0,
        "mean_confidence_when_correct": (
            sum(all_correct_conf) / len(all_correct_conf) if all_correct_conf else None
        ),
        "mean_confidence_when_incorrect": (
            sum(all_incorrect_conf) / len(all_incorrect_conf) if all_incorrect_conf else None
        ),
        "scenarios": per_scenario,
    }
    return summary


def write_chart(summary: dict, out_path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    names = [s["name"] for s in summary["scenarios"]]
    top1 = [s["top1_accuracy"] * 100 for s in summary["scenarios"]]
    top3 = [s["top3_accuracy"] * 100 for s in summary["scenarios"]]

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), gridspec_kw={"width_ratios": [3, 2]})

    ax = axes[0]
    x = range(len(names))
    width = 0.35
    ax.bar([i - width / 2 for i in x], top1, width, label="Top-1", color="#d69a3e")
    ax.bar([i + width / 2 for i in x], top3, width, label="Top-3", color="#5a7d7c")
    ax.set_xticks(list(x))
    ax.set_xticklabels([n.replace("_", "\n") for n in names], fontsize=8)
    ax.set_ylabel("Accuracy (%)")
    ax.set_ylim(0, 105)
    ax.set_title("Suspect-ranking accuracy by scenario")
    ax.legend()
    ax.axhline(100, color="#444", linewidth=0.5, linestyle="--")

    ax2 = axes[1]
    labels = ["Overall\nTop-1", "Overall\nTop-3"]
    vals = [summary["top1_accuracy"] * 100, summary["top3_accuracy"] * 100]
    ax2.bar(labels, vals, color=["#d69a3e", "#5a7d7c"])
    ax2.set_ylim(0, 105)
    ax2.set_title("Overall accuracy")
    for i, v in enumerate(vals):
        ax2.text(i, v + 2, f"{v:.0f}%", ha="center", fontsize=9)

    fig.suptitle("AUTOPSY — deterministic engine accuracy (no API key used)", fontsize=11)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=3, help="Repeats per scenario (default 3)")
    parser.add_argument("--no-chart", action="store_true", help="Skip PNG chart generation")
    args = parser.parse_args()

    OUT_DIR.mkdir(exist_ok=True)
    summary = evaluate(trials=args.trials)

    json_path = OUT_DIR / "accuracy.json"
    json_path.write_text(json.dumps(summary, indent=2))

    print(f"Overall top-1 accuracy: {summary['top1_accuracy']*100:.1f}%")
    print(f"Overall top-3 accuracy: {summary['top3_accuracy']*100:.1f}%")
    for s in summary["scenarios"]:
        print(f"  {s['name']}: top-1={s['top1_accuracy']*100:.0f}%  top-3={s['top3_accuracy']*100:.0f}%")
    print(f"\nWrote {json_path}")

    if not args.no_chart:
        try:
            chart_path = OUT_DIR / "accuracy.png"
            write_chart(summary, chart_path)
            print(f"Wrote {chart_path}")
        except ImportError:
            print("matplotlib not installed — run `pip install matplotlib` for the PNG chart, "
                  "or re-run with --no-chart. JSON results were still written.")


if __name__ == "__main__":
    main()
