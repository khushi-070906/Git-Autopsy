"""
Phase 8 — Optional AI explanation layer.

Rules enforced here per spec:
  - AI is OPTIONAL. Every function in this module degrades to a deterministic
    template-based summary if no API key / client is configured.
  - The LLM (when available) receives only structured evidence extracted
    from the Evidence Graph — never raw repository file contents, never the
    full commit list dumped uncontrolled.
  - The LLM is never asked to invent the root cause. It only explains and
    phrases conclusions that the deterministic engine already reached.
"""
from __future__ import annotations

import os
from dataclasses import asdict

from app.analysis.why_analysis import Suspect

AI_ENABLED = bool(os.environ.get("ANTHROPIC_API_KEY"))


def _template_explanation(suspect: Suspect) -> str:
    """Deterministic fallback used when no AI client is configured."""
    lines = [
        f"Commit {suspect.short_sha} is flagged with {round(suspect.confidence * 100)}% confidence.",
        suspect.likely_cause_summary,
    ]
    if suspect.affected_files:
        lines.append(f"Files affected: {', '.join(suspect.affected_files[:5])}.")
    lines.append(f"Next step: {suspect.recommendation}")
    return " ".join(lines)


def explain_suspect(suspect: Suspect) -> str:
    """
    Return a human-readable explanation of a suspect commit.
    Uses an LLM only if configured; otherwise returns the deterministic
    template. Either way, the underlying facts come from the Evidence Graph,
    not from the LLM's own knowledge.
    """
    if not AI_ENABLED:
        return _template_explanation(suspect)

    try:
        return _explain_with_llm(suspect)
    except Exception:
        # Never let an AI-layer failure break the core deterministic report.
        return _template_explanation(suspect)


def _explain_with_llm(suspect: Suspect) -> str:
    """
    Calls the Anthropic API with ONLY structured evidence (dataclass fields),
    never raw file contents or the full repository. Import is local so the
    module has no hard dependency on the `anthropic` package when AI is
    disabled.
    """
    import anthropic

    client = anthropic.Anthropic()
    structured_evidence = {
        "commit_short_sha": suspect.short_sha,
        "confidence_score": suspect.confidence,
        "affected_files": suspect.affected_files,
        "affected_functions": suspect.affected_functions,
        "evidence_chain": [
            {"kind": e.kind, "text": e.text} for e in suspect.evidence
        ],
    }
    prompt = (
        "You are explaining a software regression analysis to a developer. "
        "Below is STRUCTURED EVIDENCE already computed by a deterministic "
        "analysis engine. Do not invent any fact not present in this "
        "evidence. Write a concise (3-4 sentence) plain-English explanation "
        "of the likely cause and a one-sentence recommended next step.\n\n"
        f"Evidence: {structured_evidence}"
    )
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    text_blocks = [b.text for b in response.content if getattr(b, "type", "") == "text"]
    return "\n".join(text_blocks) if text_blocks else _template_explanation(suspect)
