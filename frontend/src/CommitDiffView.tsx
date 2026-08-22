import { useEffect, useState } from "react";
import { getCommitDiff } from "./api";

function DiffLine({ line }: { line: string }) {
  let color = "var(--dim)";
  let bg = "transparent";
  if (line.startsWith("+++") || line.startsWith("---")) {
    color = "var(--dim)";
  } else if (line.startsWith("+")) {
    color = "var(--tag-fact)";
    bg = "rgba(159, 227, 236, 0.06)";
  } else if (line.startsWith("-")) {
    color = "var(--finding)";
    bg = "rgba(209, 73, 61, 0.08)";
  } else if (line.startsWith("@@")) {
    color = "var(--tag-inference)";
  } else if (line.startsWith("diff --git") || line.startsWith("index ")) {
    color = "var(--paper)";
  }
  return (
    <div style={{ color, background: bg, whiteSpace: "pre", padding: "0 8px" }}>
      {line || " "}
    </div>
  );
}

// Fetches lazily — the backend re-clones the repo to read a diff (it isn't
// kept around after the analysis pipeline finishes) and is rate-limited
// the same way starting a new analysis is, so this only fetches once
// actually mounted (i.e. once the person expands a specific commit),
// never for every suspect up front.
export function CommitDiffView({ analysisId, sha }: { analysisId: string; sha: string }) {
  const [diff, setDiff] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDiff(null);
    setError(null);
    getCommitDiff(analysisId, sha)
      .then((d) => {
        if (!cancelled) setDiff(d.diff);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load diff.");
      });
    return () => {
      cancelled = true;
    };
  }, [analysisId, sha]);

  if (error) {
    return <div style={{ color: "var(--finding)", fontSize: 12, padding: "8px 0" }}>{error}</div>;
  }
  if (diff === null) {
    return <div style={{ color: "var(--dim)", fontSize: 12, padding: "8px 0" }}>Loading diff...</div>;
  }
  if (diff.trim() === "") {
    return <div style={{ color: "var(--dim)", fontSize: 12, padding: "8px 0" }}>No textual diff (binary change or empty commit).</div>;
  }

  return (
    <div
      style={{
        fontFamily: "var(--font-mono)",
        fontSize: 12,
        lineHeight: 1.5,
        background: "var(--bg)",
        border: "1px solid var(--hairline)",
        borderRadius: 4,
        marginTop: 8,
        maxHeight: 420,
        overflow: "auto",
      }}
    >
      {diff.split("\n").map((line, i) => (
        <DiffLine key={i} line={line} />
      ))}
    </div>
  );
}
