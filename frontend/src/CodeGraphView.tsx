import { useEffect, useMemo, useRef, useState } from "react";
import { GraphEdge, GraphNode, getCodeGraph } from "./api";

// No layout library is added here (the frontend deliberately only depends
// on react/react-dom) — this runs a small, self-contained force
// simulation synchronously on load, then renders a static SVG. Drag
// interactions move a node directly rather than re-running physics, which
// keeps this predictable inside React's render cycle instead of fighting
// a persistent animation loop against React state.

interface SimNode {
  id: string;
  label: string;
  kind: string;
  isTest: boolean;
  x: number;
  y: number;
}

const NODE_COLOR = {
  file: "var(--tag-fact)",
  fileTest: "var(--tag-recommendation)",
  function: "var(--tag-inference)",
};

const EDGE_COLOR: Record<string, string> = {
  IMPORTS: "var(--tag-evidence)",
  CALLS: "var(--dim)",
  FILE_CONTAINS_FUNCTION: "var(--hairline)",
};

function shortLabel(n: GraphNode): string {
  const path = String(n.path ?? n.id);
  const base = path.split("/").pop() || path;
  return n.kind === "function" ? String(n.name ?? base) : base;
}

function isTestPath(n: GraphNode): boolean {
  const p = String(n.path ?? n.file ?? n.id);
  return /test/i.test(p);
}

// Aggregates function-level CALLS edges up to their containing files (via
// FILE_CONTAINS_FUNCTION) so the default view reads as a repo map rather
// than a dense function-level tangle. Toggled off by "Show functions".
function toFileLevel(nodes: GraphNode[], edges: GraphEdge[]): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const fileOf: Record<string, string> = {};
  for (const e of edges) {
    if (e.kind === "FILE_CONTAINS_FUNCTION") fileOf[e.target] = e.source;
  }
  const fileNodes = nodes.filter((n) => n.kind === "file");
  const seen = new Set<string>();
  const fileEdges: GraphEdge[] = [];
  for (const e of edges) {
    if (e.kind === "IMPORTS") {
      const key = `${e.source}->${e.target}:IMPORTS`;
      if (!seen.has(key)) {
        seen.add(key);
        fileEdges.push(e);
      }
    } else if (e.kind === "CALLS") {
      const sf = fileOf[e.source];
      const tf = fileOf[e.target];
      if (sf && tf && sf !== tf) {
        const key = `${sf}->${tf}:CALLS`;
        if (!seen.has(key)) {
          seen.add(key);
          fileEdges.push({ source: sf, target: tf, kind: "CALLS" });
        }
      }
    }
  }
  return { nodes: fileNodes, edges: fileEdges };
}

const W = 860;
const H = 560;

function layout(nodes: GraphNode[], edges: GraphEdge[]): SimNode[] {
  const sim: SimNode[] = nodes.map((n, i) => {
    const angle = (i / Math.max(nodes.length, 1)) * Math.PI * 2;
    return {
      id: n.id,
      label: shortLabel(n),
      kind: n.kind,
      isTest: isTestPath(n),
      x: W / 2 + Math.cos(angle) * 120 + (Math.random() - 0.5) * 20,
      y: H / 2 + Math.sin(angle) * 120 + (Math.random() - 0.5) * 20,
    };
  });
  const byId: Record<string, SimNode> = {};
  sim.forEach((n) => (byId[n.id] = n));
  const links = edges
    .map((e) => ({ a: byId[e.source], b: byId[e.target] }))
    .filter((l) => l.a && l.b);

  const iterations = sim.length > 120 ? 150 : 300;
  for (let it = 0; it < iterations; it++) {
    // repulsion, capped distance so far-apart nodes don't drift forever
    for (let i = 0; i < sim.length; i++) {
      for (let j = i + 1; j < sim.length; j++) {
        const a = sim[i], b = sim[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        let dist = Math.sqrt(dx * dx + dy * dy) || 1;
        if (dist > 220) continue;
        const force = (700 / (dist * dist)) * 4;
        dx = (dx / dist) * force;
        dy = (dy / dist) * force;
        a.x += dx; a.y += dy;
        b.x -= dx; b.y -= dy;
      }
    }
    // spring along edges
    for (const { a, b } of links) {
      let dx = b.x - a.x, dy = b.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const target = 90;
      const force = (dist - target) * 0.02;
      dx = (dx / dist) * force;
      dy = (dy / dist) * force;
      a.x += dx; a.y += dy;
      b.x -= dx; b.y -= dy;
    }
    // gentle pull to center + clamp inside viewBox
    for (const n of sim) {
      n.x += (W / 2 - n.x) * 0.004;
      n.y += (H / 2 - n.y) * 0.004;
      n.x = Math.max(24, Math.min(W - 24, n.x));
      n.y = Math.max(24, Math.min(H - 24, n.y));
    }
  }
  return sim;
}

export function CodeGraphView({ analysisId }: { analysisId: string }) {
  const [raw, setRaw] = useState<{ nodes: GraphNode[]; edges: GraphEdge[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showFunctions, setShowFunctions] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [positions, setPositions] = useState<SimNode[] | null>(null);
  const dragId = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setRaw(null);
    setError(null);
    getCodeGraph(analysisId)
      .then((g) => {
        if (!cancelled) setRaw(g);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load code graph.");
      });
    return () => {
      cancelled = true;
    };
  }, [analysisId]);

  const view = useMemo(() => {
    if (!raw) return null;
    return showFunctions ? raw : toFileLevel(raw.nodes, raw.edges);
  }, [raw, showFunctions]);

  useEffect(() => {
    if (!view) return;
    setSelected(null);
    setPositions(layout(view.nodes, view.edges));
  }, [view]);

  const nodesById = useMemo(() => {
    const m: Record<string, SimNode> = {};
    (positions || []).forEach((n) => (m[n.id] = n));
    return m;
  }, [positions]);

  const connected = useMemo(() => {
    if (!selected || !view) return null;
    const s = new Set<string>([selected]);
    for (const e of view.edges) {
      if (e.source === selected) s.add(e.target);
      if (e.target === selected) s.add(e.source);
    }
    return s;
  }, [selected, view]);

  const onPointerMove = (ev: React.PointerEvent<SVGSVGElement>) => {
    if (!dragId.current || !positions) return;
    const svg = ev.currentTarget;
    const rect = svg.getBoundingClientRect();
    const x = ((ev.clientX - rect.left) / rect.width) * W;
    const y = ((ev.clientY - rect.top) / rect.height) * H;
    setPositions((prev) =>
      (prev || []).map((n) => (n.id === dragId.current ? { ...n, x, y } : n))
    );
  };

  if (error) return <div style={{ color: "#c24a3f", fontSize: 13 }}>{error}</div>;
  if (!raw || !view || !positions) {
    return <div style={{ color: "var(--paper-dim)", fontSize: 13 }}>Loading dependency graph...</div>;
  }

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 10, fontSize: 11 }}>
        <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", color: "var(--paper-dim)" }}>
          <input
            type="checkbox"
            checked={showFunctions}
            onChange={(e) => setShowFunctions(e.target.checked)}
          />
          Show functions
        </label>
        <span style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--paper-dim)" }}>
          <span style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--tag-fact)", display: "inline-block" }} />
          file
        </span>
        <span style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--paper-dim)" }}>
          <span style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--tag-recommendation)", display: "inline-block" }} />
          test file
        </span>
        {showFunctions && (
          <span style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--paper-dim)" }}>
            <span style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--tag-inference)", display: "inline-block" }} />
            function
          </span>
        )}
        <span style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--paper-dim)" }}>
          <span style={{ width: 14, height: 1, background: "var(--tag-evidence)", display: "inline-block" }} />
          imports
        </span>
        <span style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--paper-dim)" }}>
          <span style={{ width: 14, height: 1, background: "var(--dim)", display: "inline-block" }} />
          calls
        </span>
      </div>

      <svg
        viewBox={`0 0 ${W} ${H}`}
        style={{ width: "100%", height: 560, background: "var(--panel)", border: "1px solid var(--hairline)", borderRadius: 4 }}
        onPointerMove={onPointerMove}
        onPointerUp={() => (dragId.current = null)}
        onPointerLeave={() => (dragId.current = null)}
        onClick={(e) => {
          if (e.target === e.currentTarget) setSelected(null);
        }}
      >
        {view.edges.map((e, i) => {
          const a = nodesById[e.source];
          const b = nodesById[e.target];
          if (!a || !b) return null;
          const dim = connected && !(connected.has(e.source) && connected.has(e.target));
          return (
            <line
              key={i}
              x1={a.x} y1={a.y} x2={b.x} y2={b.y}
              stroke={EDGE_COLOR[e.kind] || "var(--hairline)"}
              strokeWidth={e.kind === "IMPORTS" ? 1 : 0.6}
              opacity={dim ? 0.06 : e.kind === "FILE_CONTAINS_FUNCTION" ? 0.25 : 0.5}
            />
          );
        })}
        {positions.map((n) => {
          const dim = connected && !connected.has(n.id);
          const color = n.kind === "function" ? NODE_COLOR.function : n.isTest ? NODE_COLOR.fileTest : NODE_COLOR.file;
          return (
            <g
              key={n.id}
              transform={`translate(${n.x},${n.y})`}
              style={{ cursor: "pointer" }}
              opacity={dim ? 0.2 : 1}
              onPointerDown={() => (dragId.current = n.id)}
              onClick={(e) => {
                e.stopPropagation();
                setSelected(n.id === selected ? null : n.id);
              }}
            >
              <circle r={n.kind === "function" ? 3.5 : 6} fill={color} stroke="var(--panel)" strokeWidth={1.5} />
              {(selected === n.id || (!connected && n.kind === "file")) && (
                <text
                  x={9} y={3}
                  fontFamily="var(--font-mono)"
                  fontSize={10}
                  fill="var(--paper-dim)"
                >
                  {n.label}
                </text>
              )}
              {connected && connected.has(n.id) && n.id !== selected && (
                <text x={9} y={3} fontFamily="var(--font-mono)" fontSize={10} fill="var(--paper)">
                  {n.label}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <div style={{ fontSize: 11, color: "var(--paper-dim)", marginTop: 6 }}>
        {view.nodes.length} nodes, {view.edges.length} edges. Drag to rearrange, click a node to trace its
        connections.
      </div>
    </div>
  );
}
