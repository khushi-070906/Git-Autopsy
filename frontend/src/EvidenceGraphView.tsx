import { useEffect, useMemo, useRef, useState } from "react";
import {
  GraphEdge,
  GraphNode,
  CounterfactualJob,
  startCounterfactual,
  getCounterfactualJob,
} from "./api";

const KIND_COLOR: Record<string, string> = {
  commit: "var(--tag-fact)",
  file: "var(--paper-dim)",
  function: "var(--tag-inference)",
  dependency: "var(--tag-evidence)",
  test: "var(--tag-recommendation)",
  author: "#8a93a0",
};

// Edge kinds treated as a verified/near-verified causal claim rather than
// a plain structural relationship (a commit touching a file, a file
// containing a function). These render distinctly in the evidence list —
// bold + colored instead of the default muted row — so a causal link
// doesn't read the same as "this file happens to contain this function".
const CAUSAL_EDGE_KINDS = new Set(["COMMIT_CHANGED_DEPENDENCY"]);

// suspect_confidence (0-1) -> ring color. Written onto commit nodes by
// the backend's evidence_graph.annotate_suspect_confidence(), called from
// pipeline.py after why_analysis.rank_suspects(). Nodes with no
// suspect_confidence (not ranked as a suspect at all) get no ring.
function confidenceColor(confidence: number): string {
  if (confidence >= 0.75) return "#c24a3f";
  if (confidence >= 0.5) return "#d9776a";
  if (confidence >= 0.3) return "#e8a89e";
  return "#f0cec7";
}

function label(n: GraphNode): string {
  switch (n.kind) {
    case "commit":
      return String(n.short_sha ?? n.id);
    case "file":
      return String(n.path ?? n.id);
    case "function":
      return String(n.name ?? n.id);
    case "dependency":
      return String(n.name ?? n.id);
    case "author":
      return String(n.name ?? n.id);
    case "test":
      return String(n.path ?? n.id);
    default:
      return n.id;
  }
}

const POLL_INTERVAL_MS = 2500;

function CounterfactualPanel({
  analysisId,
  commitSha,
  onComplete,
}: {
  analysisId: string;
  commitSha: string;
  onComplete?: () => void;
}) {
  const [job, setJob] = useState<CounterfactualJob | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  // Reset state whenever the selected commit changes, and stop any
  // in-flight poll from the previously selected commit — otherwise a
  // stale timer can overwrite this commit's panel with the old commit's
  // result a few seconds after switching selection.
  useEffect(() => {
    setJob(null);
    setStartError(null);
    if (pollRef.current) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [commitSha]);

  const runReplay = async () => {
    setStarting(true);
    setStartError(null);
    try {
      const { job_id } = await startCounterfactual(analysisId, commitSha);
      setJob({ status: "queued", error: null, result: null });
      pollRef.current = window.setInterval(async () => {
        try {
          const j = await getCounterfactualJob(job_id);
          setJob(j);
          if (j.status === "completed" || j.status === "failed") {
            if (pollRef.current) {
              window.clearInterval(pollRef.current);
              pollRef.current = null;
            }
            // Backend persists confirmed/ruled-out outcomes into the
            // stored Analysis row (see main.py's _run_counterfactual_job
            // -> regression_detection.apply_counterfactual_result). This
            // tells the parent to re-fetch getAnalysis so SECONDARY
            // FINDINGS picks it up immediately instead of only after a
            // manual page reload. Fired for "failed" too so a stale
            // in-flight indicator elsewhere doesn't linger, even though
            // a failed replay writes nothing new server-side.
            if (j.status === "completed") onComplete?.();
          }
        } catch (err) {
          setStartError(err instanceof Error ? err.message : "Failed to poll job status.");
          if (pollRef.current) {
            window.clearInterval(pollRef.current);
            pollRef.current = null;
          }
        }
      }, POLL_INTERVAL_MS);
    } catch (err) {
      setStartError(err instanceof Error ? err.message : "Failed to start replay.");
    } finally {
      setStarting(false);
    }
  };

  return (
    <div style={{ marginTop: 10, marginBottom: 12, borderTop: "1px solid var(--hairline)", paddingTop: 10 }}>
      <div className="mono-label" style={{ marginBottom: 6 }}>Counterfactual replay</div>

      {!job && (
        <>
          <div style={{ fontSize: 12, color: "var(--paper-dim)", marginBottom: 8 }}>
            Re-runs the test suite with this commit reverted, to check whether removing it
            actually eliminates a failing test — a verified result, not a heuristic.
          </div>
          <button
            onClick={runReplay}
            disabled={starting}
            style={{
              fontSize: 12,
              fontFamily: "var(--font-mono)",
              padding: "6px 10px",
              borderRadius: 3,
              border: "1px solid var(--hairline)",
              background: "var(--panel-raised)",
              color: "var(--paper)",
              cursor: starting ? "default" : "pointer",
              opacity: starting ? 0.6 : 1,
            }}
          >
            {starting ? "Starting..." : "Verify with test replay"}
          </button>
          {startError && (
            <div style={{ fontSize: 12, color: "#c24a3f", marginTop: 6 }}>{startError}</div>
          )}
        </>
      )}

      {job && (job.status === "queued" || job.status === "running") && (
        <div style={{ fontSize: 12, color: "var(--paper-dim)" }}>
          {job.status === "queued" ? "Queued..." : "Running test suite with and without this commit..."}
        </div>
      )}

      {job && job.status === "failed" && (
        <div style={{ fontSize: 12, color: "#c24a3f" }}>
          Replay failed: {job.error || "unknown error"}
        </div>
      )}

      {job && job.status === "completed" && job.result && (
        <div
          style={{
            fontSize: 12,
            padding: 8,
            borderRadius: 3,
            border: `1px solid ${job.result.removes_failure ? "#c24a3f" : "var(--hairline)"}`,
            background: job.result.removes_failure ? "rgba(194,74,63,0.08)" : "var(--panel-raised)",
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: 4, color: job.result.removes_failure ? "#c24a3f" : "var(--paper)" }}>
            {job.result.removes_failure
              ? "Confirmed — reverting this commit eliminates the failure"
              : "Not confirmed — reverting this commit did not eliminate the failure"}
          </div>
          <div style={{ color: "var(--paper-dim)" }}>
            Framework: {job.result.framework}
          </div>
          {job.result.baseline_failing_tests.length > 0 && (
            <div style={{ color: "var(--paper-dim)", marginTop: 4 }}>
              Failing with commit: {job.result.baseline_failing_tests.join(", ")}
            </div>
          )}
          {job.result.without_commit_failing_tests.length > 0 && (
            <div style={{ color: "var(--paper-dim)", marginTop: 2 }}>
              Still failing without commit: {job.result.without_commit_failing_tests.join(", ")}
            </div>
          )}
          {(job.result.baseline_timed_out || job.result.without_commit_timed_out) && (
            <div style={{ color: "#c24a3f", marginTop: 4 }}>
              One or both test runs timed out — result may be inconclusive.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function EvidenceGraphView({
  analysisId,
  nodes,
  edges,
  onCounterfactualComplete,
}: {
  analysisId: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  onCounterfactualComplete?: () => void;
}) {
  const [selected, setSelected] = useState<string | null>(null);

  // Suspect commits first within their kind group, ranked by confidence —
  // so the nodes most worth clicking are the ones a reviewer sees first,
  // not buried alphabetically/by-id among ordinary commits.
  const grouped = useMemo(() => {
    const g: Record<string, GraphNode[]> = {};
    for (const n of nodes) {
      g[n.kind] = g[n.kind] || [];
      g[n.kind].push(n);
    }
    for (const kind of Object.keys(g)) {
      g[kind].sort((a, b) => {
        const ca = typeof a.suspect_confidence === "number" ? a.suspect_confidence : -1;
        const cb = typeof b.suspect_confidence === "number" ? b.suspect_confidence : -1;
        return cb - ca;
      });
    }
    return g;
  }, [nodes]);

  const { incoming, outgoing } = useMemo(() => {
    if (!selected) return { incoming: [] as GraphEdge[], outgoing: [] as GraphEdge[] };
    return {
      incoming: edges.filter((e) => e.target === selected),
      outgoing: edges.filter((e) => e.source === selected),
    };
  }, [selected, edges]);

  const nodesById = useMemo(() => {
    const m: Record<string, GraphNode> = {};
    for (const n of nodes) m[n.id] = n;
    return m;
  }, [nodes]);

  const selectedNode = selected ? nodesById[selected] : null;
  const selectedConfidence =
    selectedNode && typeof selectedNode.suspect_confidence === "number"
      ? (selectedNode.suspect_confidence as number)
      : null;
  const selectedSummary =
    selectedNode && typeof selectedNode.suspect_summary === "string"
      ? (selectedNode.suspect_summary as string)
      : null;
  // Counterfactual replay only makes sense for a commit node — sha is
  // stored as `sha` on commit nodes (see evidence_graph.py's
  // g.add_node(f"commit:{c.sha}", kind="commit", sha=c.sha, ...)).
  const selectedCommitSha =
    selectedNode && selectedNode.kind === "commit" && typeof selectedNode.sha === "string"
      ? (selectedNode.sha as string)
      : null;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr", gap: 20 }}>
      <div>
        {Object.entries(grouped).map(([kind, kindNodes]) => (
          <div key={kind} style={{ marginBottom: 16 }}>
            <div className="mono-label" style={{ marginBottom: 8, color: KIND_COLOR[kind] || "var(--paper-dim)" }}>
              {kind} · {kindNodes.length}
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {kindNodes.slice(0, 60).map((n) => {
                const confidence =
                  typeof n.suspect_confidence === "number" ? (n.suspect_confidence as number) : null;
                const ring = confidence != null ? confidenceColor(confidence) : null;
                return (
                  <button
                    key={n.id}
                    onClick={() => setSelected(n.id)}
                    title={n.id}
                    style={{
                      background: selected === n.id ? (KIND_COLOR[kind] || "#666") : "var(--panel-raised)",
                      color: selected === n.id ? "var(--ink)" : "var(--paper)",
                      border: `${ring ? 2 : 1}px solid ${ring || KIND_COLOR[kind] || "var(--hairline)"}`,
                      borderRadius: 3,
                      padding: "4px 8px",
                      fontSize: 11,
                      fontFamily: "var(--font-mono)",
                      maxWidth: 220,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                      position: "relative",
                    }}
                  >
                    {label(n)}
                    {confidence != null && ` · ${Math.round(confidence * 100)}%`}
                  </button>
                );
              })}
              {kindNodes.length > 60 && (
                <span style={{ fontSize: 11, color: "var(--paper-dim)", alignSelf: "center" }}>
                  +{kindNodes.length - 60} more
                </span>
              )}
            </div>
          </div>
        ))}
      </div>

      <div
        style={{
          background: "var(--panel)",
          border: "1px solid var(--hairline)",
          borderRadius: 4,
          padding: 16,
          position: "sticky",
          top: 16,
          maxHeight: 560,
          overflowY: "auto",
        }}
      >
        <div className="mono-label" style={{ marginBottom: 10 }}>Evidence for node</div>
        {!selected && (
          <div style={{ color: "var(--paper-dim)", fontSize: 13 }}>
            Click any node on the left to inspect its supporting evidence — every incoming and
            outgoing relationship recorded in the graph. Commit nodes with a confidence percentage
            are ranked suspects; darker borders mean higher confidence.
          </div>
        )}
        {selected && (
          <div>
            <div style={{ fontFamily: "var(--font-display)", fontSize: 17, marginBottom: 10, wordBreak: "break-all" }}>
              {label(nodesById[selected]) || selected}
            </div>

            {selectedConfidence != null && (
              <div
                style={{
                  fontSize: 12,
                  fontWeight: 600,
                  color: confidenceColor(selectedConfidence),
                  marginBottom: 6,
                }}
              >
                {Math.round(selectedConfidence * 100)}% suspect confidence
              </div>
            )}
            {selectedSummary && (
              <div
                style={{
                  fontSize: 12,
                  color: "var(--paper)",
                  background: "var(--panel-raised)",
                  border: "1px solid var(--hairline)",
                  borderRadius: 3,
                  padding: 8,
                  marginBottom: 10,
                }}
              >
                {selectedSummary}
              </div>
            )}

            {selectedCommitSha && (
              <CounterfactualPanel
                analysisId={analysisId}
                commitSha={selectedCommitSha}
                onComplete={onCounterfactualComplete}
              />
            )}

            <div className="mono-label" style={{ marginTop: 12, marginBottom: 6 }}>Incoming ({incoming.length})</div>
            {incoming.length === 0 && <div style={{ fontSize: 12, color: "var(--paper-dim)" }}>None recorded.</div>}
            {incoming.map((e, i) => {
              const causal = CAUSAL_EDGE_KINDS.has(e.kind);
              return (
                <div
                  key={i}
                  style={{
                    fontSize: 12,
                    padding: "4px 0",
                    borderBottom: "1px solid var(--hairline)",
                    fontWeight: causal ? 600 : 400,
                  }}
                >
                  <span style={{ color: causal ? "var(--tag-evidence)" : "var(--tag-fact)" }}>{e.kind}</span>{" "}
                  <span style={{ color: "var(--paper-dim)" }}>from</span>{" "}
                  {label(nodesById[e.source] || { id: e.source, kind: "" })}
                </div>
              );
            })}
            <div className="mono-label" style={{ marginTop: 12, marginBottom: 6 }}>Outgoing ({outgoing.length})</div>
            {outgoing.length === 0 && <div style={{ fontSize: 12, color: "var(--paper-dim)" }}>None recorded.</div>}
            {outgoing.map((e, i) => {
              const causal = CAUSAL_EDGE_KINDS.has(e.kind);
              return (
                <div
                  key={i}
                  style={{
                    fontSize: 12,
                    padding: "4px 0",
                    borderBottom: "1px solid var(--hairline)",
                    fontWeight: causal ? 600 : 400,
                  }}
                >
                  <span style={{ color: causal ? "var(--tag-evidence)" : "var(--tag-fact)" }}>{e.kind}</span>{" "}
                  <span style={{ color: "var(--paper-dim)" }}>to</span>{" "}
                  {label(nodesById[e.target] || { id: e.target, kind: "" })}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
