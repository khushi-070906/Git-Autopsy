import { useMemo, useState } from "react";
import { GraphEdge, GraphNode } from "./api";

const KIND_COLOR: Record<string, string> = {
  commit: "var(--tag-fact)",
  file: "var(--paper-dim)",
  function: "var(--tag-inference)",
  dependency: "var(--tag-evidence)",
  test: "var(--tag-recommendation)",
  author: "#8a93a0",
};

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

export function EvidenceGraphView({ nodes, edges }: { nodes: GraphNode[]; edges: GraphEdge[] }) {
  const [selected, setSelected] = useState<string | null>(null);

  const grouped = useMemo(() => {
    const g: Record<string, GraphNode[]> = {};
    for (const n of nodes) {
      g[n.kind] = g[n.kind] || [];
      g[n.kind].push(n);
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

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr", gap: 20 }}>
      <div>
        {Object.entries(grouped).map(([kind, kindNodes]) => (
          <div key={kind} style={{ marginBottom: 16 }}>
            <div className="mono-label" style={{ marginBottom: 8, color: KIND_COLOR[kind] || "var(--paper-dim)" }}>
              {kind} · {kindNodes.length}
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {kindNodes.slice(0, 60).map((n) => (
                <button
                  key={n.id}
                  onClick={() => setSelected(n.id)}
                  title={n.id}
                  style={{
                    background: selected === n.id ? (KIND_COLOR[kind] || "#666") : "var(--panel-raised)",
                    color: selected === n.id ? "var(--ink)" : "var(--paper)",
                    border: `1px solid ${KIND_COLOR[kind] || "var(--hairline)"}`,
                    borderRadius: 3,
                    padding: "4px 8px",
                    fontSize: 11,
                    fontFamily: "var(--font-mono)",
                    maxWidth: 220,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {label(n)}
                </button>
              ))}
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
          maxHeight: 480,
          overflowY: "auto",
        }}
      >
        <div className="mono-label" style={{ marginBottom: 10 }}>Evidence for node</div>
        {!selected && (
          <div style={{ color: "var(--paper-dim)", fontSize: 13 }}>
            Click any node on the left to inspect its supporting evidence — every incoming and
            outgoing relationship recorded in the graph.
          </div>
        )}
        {selected && (
          <div>
            <div style={{ fontFamily: "var(--font-display)", fontSize: 17, marginBottom: 10, wordBreak: "break-all" }}>
              {label(nodesById[selected]) || selected}
            </div>
            <div className="mono-label" style={{ marginTop: 12, marginBottom: 6 }}>Incoming ({incoming.length})</div>
            {incoming.length === 0 && <div style={{ fontSize: 12, color: "var(--paper-dim)" }}>None recorded.</div>}
            {incoming.map((e, i) => (
              <div key={i} style={{ fontSize: 12, padding: "4px 0", borderBottom: "1px solid var(--hairline)" }}>
                <span style={{ color: "var(--tag-fact)" }}>{e.kind}</span>{" "}
                <span style={{ color: "var(--paper-dim)" }}>from</span>{" "}
                {label(nodesById[e.source] || { id: e.source, kind: "" })}
              </div>
            ))}
            <div className="mono-label" style={{ marginTop: 12, marginBottom: 6 }}>Outgoing ({outgoing.length})</div>
            {outgoing.length === 0 && <div style={{ fontSize: 12, color: "var(--paper-dim)" }}>None recorded.</div>}
            {outgoing.map((e, i) => (
              <div key={i} style={{ fontSize: 12, padding: "4px 0", borderBottom: "1px solid var(--hairline)" }}>
                <span style={{ color: "var(--tag-fact)" }}>{e.kind}</span>{" "}
                <span style={{ color: "var(--paper-dim)" }}>to</span>{" "}
                {label(nodesById[e.target] || { id: e.target, kind: "" })}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
