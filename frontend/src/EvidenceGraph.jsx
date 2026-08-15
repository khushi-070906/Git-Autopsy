import React, { useEffect, useMemo, useState, useCallback } from "react";
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  Handle,
  Position,
  MarkerType,
} from "reactflow";
import "reactflow/dist/style.css";

// Layout: simple lane-by-kind layout rather than a force/dagre layout.
// The evidence graph's node kinds (commit, file, function, dependency,
// test, author) form a fairly shallow, wide structure rather than a deep
// tree, so lanes read clearer than an auto-layout algorithm fighting
// itself. Swap for dagre/elk later if graphs get large enough that lanes
// overlap within a kind.
const LANE_ORDER = ["author", "commit", "file", "dependency", "function", "test"];
const LANE_X = { author: 0, commit: 260, file: 520, dependency: 780, function: 1040, test: 1300 };
const LANE_Y_GAP = 90;

const KIND_COLOR = {
  commit: { bg: "#E6F1FB", border: "#378ADD", text: "#0C447C" },
  file: { bg: "#F1EFE8", border: "#888780", text: "#444441" },
  function: { bg: "#EEEDFE", border: "#7F77DD", text: "#3C3489" },
  dependency: { bg: "#FAECE7", border: "#D85A30", text: "#712B13" },
  test: { bg: "#E1F5EE", border: "#1D9E75", text: "#085041" },
  author: { bg: "#FBEAF0", border: "#D4537E", text: "#72243E" },
};

// Suspect commits get an additional red-intensity ring on top of their
// normal kind color — confidence closer to 1 reads as a stronger, more
// saturated border. This is the piece that makes the graph show WHERE the
// suspected regression is at a glance, not just what's structurally
// connected to what.
function confidenceRingColor(confidence) {
  if (confidence == null) return null;
  if (confidence >= 0.75) return "#A32D2D";
  if (confidence >= 0.5) return "#E24B4A";
  if (confidence >= 0.3) return "#F09595";
  return "#F7C1C1";
}

function nodeLabel(node) {
  switch (node.kind) {
    case "commit":
      return node.short_sha || node.sha?.slice(0, 8) || "commit";
    case "file":
      return node.path?.split("/").pop() || node.path || "file";
    case "function":
      return node.name || "function";
    case "dependency":
      return node.name || "dependency";
    case "test":
      return node.path?.split("/").pop() || "test";
    case "author":
      return node.name || node.email || "author";
    default:
      return node.id;
  }
}

function EvidenceNode({ data }) {
  const colors = KIND_COLOR[data.kind] || KIND_COLOR.file;
  const ring = confidenceRingColor(data.suspect_confidence);
  return (
    <div
      style={{
        background: colors.bg,
        border: `${ring ? 2 : 1}px solid ${ring || colors.border}`,
        borderRadius: 8,
        padding: "8px 12px",
        minWidth: 120,
        maxWidth: 200,
        cursor: "pointer",
        boxShadow: ring ? `0 0 0 2px ${ring}22` : "none",
      }}
    >
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div style={{ fontSize: 11, color: colors.text, opacity: 0.7, textTransform: "uppercase" }}>
        {data.kind}
      </div>
      <div style={{ fontSize: 13, color: colors.text, fontWeight: 500, wordBreak: "break-word" }}>
        {data.label}
      </div>
      {data.suspect_confidence != null && (
        <div style={{ fontSize: 11, color: ring, fontWeight: 500, marginTop: 2 }}>
          {Math.round(data.suspect_confidence * 100)}% suspect
        </div>
      )}
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  );
}

const nodeTypes = { evidence: EvidenceNode };

function layoutNodes(rawNodes) {
  const laneCounts = {};
  return rawNodes.map((n) => {
    const kind = n.kind || "file";
    const lane = laneCounts[kind] || 0;
    laneCounts[kind] = lane + 1;
    return {
      id: n.id,
      type: "evidence",
      position: { x: LANE_X[kind] ?? 0, y: lane * LANE_Y_GAP + 40 },
      data: { ...n, label: nodeLabel(n) },
    };
  });
}

// Edge kinds that represent verified causal claims get drawn solid;
// everything else (structural relationships like FILE_CONTAINS_FUNCTION,
// COMMIT_AUTHORED_BY) is dashed. This is the "solid vs dashed" causal
// distinction — currently CI-confirmed and counterfactual-confirmed edges
// are the only ones eligible; extend CAUSAL_EDGE_KINDS as more evidence
// sources are wired into the graph.
const CAUSAL_EDGE_KINDS = new Set(["COMMIT_CHANGED_DEPENDENCY"]);

function layoutEdges(rawEdges) {
  return rawEdges.map((e, i) => {
    const causal = CAUSAL_EDGE_KINDS.has(e.kind);
    return {
      id: `${e.source}-${e.target}-${i}`,
      source: e.source,
      target: e.target,
      animated: false,
      style: {
        stroke: causal ? "#D85A30" : "#B4B2A9",
        strokeWidth: causal ? 2 : 1,
        strokeDasharray: causal ? undefined : "4 3",
      },
      markerEnd: { type: MarkerType.ArrowClosed, color: causal ? "#D85A30" : "#B4B2A9" },
    };
  });
}

function EvidencePanel({ nodeId, evidence, loading, error, onClose }) {
  if (!nodeId) return null;
  return (
    <div
      style={{
        position: "absolute",
        top: 16,
        right: 16,
        width: 320,
        maxHeight: "calc(100% - 32px)",
        overflowY: "auto",
        background: "#fff",
        border: "1px solid #D3D1C7",
        borderRadius: 12,
        padding: 16,
        boxShadow: "0 4px 16px rgba(0,0,0,0.08)",
        fontSize: 13,
        zIndex: 10,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <strong style={{ fontSize: 14 }}>Evidence</strong>
        <button onClick={onClose} style={{ border: "none", background: "none", cursor: "pointer", fontSize: 16 }}>
          x
        </button>
      </div>

      {loading && <div style={{ color: "#888780" }}>Loading...</div>}
      {error && <div style={{ color: "#A32D2D" }}>{error}</div>}

      {evidence && !loading && !error && (
        <>
          <div style={{ marginBottom: 12 }}>
            <div style={{ color: "#888780", fontSize: 11, textTransform: "uppercase" }}>
              {evidence.node.kind}
            </div>
            <div style={{ fontWeight: 500 }}>{nodeLabel(evidence.node)}</div>
            {evidence.node.message && (
              <div style={{ color: "#5F5E5A", marginTop: 4 }}>{evidence.node.message}</div>
            )}
            {evidence.node.suspect_summary && (
              <div style={{ marginTop: 8, padding: 8, background: "#FAECE7", borderRadius: 6, color: "#712B13" }}>
                {evidence.node.suspect_summary}
              </div>
            )}
          </div>

          {evidence.incoming.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <div style={{ fontWeight: 500, marginBottom: 4 }}>Incoming</div>
              {evidence.incoming.map((e, i) => (
                <div key={i} style={{ padding: "4px 0", borderTop: "1px solid #EEEDE4", color: "#5F5E5A" }}>
                  <span style={{ color: "#888780" }}>{e.kind}</span> from {e.from}
                </div>
              ))}
            </div>
          )}

          {evidence.outgoing.length > 0 && (
            <div>
              <div style={{ fontWeight: 500, marginBottom: 4 }}>Outgoing</div>
              {evidence.outgoing.map((e, i) => (
                <div key={i} style={{ padding: "4px 0", borderTop: "1px solid #EEEDE4", color: "#5F5E5A" }}>
                  <span style={{ color: "#888780" }}>{e.kind}</span> to {e.to}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// apiBase should point at the FastAPI backend, e.g. "" if served from the
// same origin behind a proxy, or "https://your-api.up.railway.app".
export default function EvidenceGraph({ analysisId, apiBase = "" }) {
  const [graph, setGraph] = useState(null);
  const [graphError, setGraphError] = useState(null);
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [evidence, setEvidence] = useState(null);
  const [evidenceLoading, setEvidenceLoading] = useState(false);
  const [evidenceError, setEvidenceError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setGraph(null);
    setGraphError(null);
    fetch(`${apiBase}/api/analysis/${analysisId}/graph`)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load graph (${res.status})`);
        return res.json();
      })
      .then((data) => {
        if (!cancelled) setGraph(data);
      })
      .catch((err) => {
        if (!cancelled) setGraphError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [analysisId, apiBase]);

  const { nodes, edges } = useMemo(() => {
    if (!graph) return { nodes: [], edges: [] };
    return { nodes: layoutNodes(graph.nodes), edges: layoutEdges(graph.edges) };
  }, [graph]);

  const onNodeClick = useCallback(
    (_, node) => {
      setSelectedNodeId(node.id);
      setEvidence(null);
      setEvidenceError(null);
      setEvidenceLoading(true);
      fetch(`${apiBase}/api/analysis/${analysisId}/graph/node/${encodeURIComponent(node.id)}`)
        .then((res) => {
          if (!res.ok) throw new Error(`Failed to load node evidence (${res.status})`);
          return res.json();
        })
        .then((data) => setEvidence(data))
        .catch((err) => setEvidenceError(err.message))
        .finally(() => setEvidenceLoading(false));
    },
    [analysisId, apiBase]
  );

  if (graphError) {
    return <div style={{ padding: 24, color: "#A32D2D" }}>{graphError}</div>;
  }
  if (!graph) {
    return <div style={{ padding: 24, color: "#888780" }}>Loading evidence graph...</div>;
  }

  return (
    <div style={{ position: "relative", width: "100%", height: "70vh", border: "1px solid #D3D1C7", borderRadius: 12 }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodeClick={onNodeClick}
        fitView
        minZoom={0.2}
      >
        <Background gap={24} color="#EEEDE4" />
        <Controls />
        <MiniMap
          nodeColor={(n) => KIND_COLOR[n.data?.kind]?.border || "#B4B2A9"}
          pannable
          zoomable
        />
      </ReactFlow>

      <EvidencePanel
        nodeId={selectedNodeId}
        evidence={evidence}
        loading={evidenceLoading}
        error={evidenceError}
        onClose={() => setSelectedNodeId(null)}
      />
    </div>
  );
}
