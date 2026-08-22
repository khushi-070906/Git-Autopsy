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
  degree: number;
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

// Large function-level graphs can run into the thousands of nodes — well
// past what's readable (or fast) as a hand-rolled force layout. Past this
// count we keep only the most-connected nodes and say so, rather than
// silently making the browser tab hang.
const MAX_RENDERED_NODES = 400;

function shortLabel(n: GraphNode): string {
  const path = String(n.path ?? n.file ?? n.id);
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

// Caps to the MAX_RENDERED_NODES most-connected nodes (by degree across
// the full edge set, computed before capping so it reflects true
// importance, not importance-after-truncation) and drops any edge that
// now dangles. Returns the cap info so the UI can say what happened.
function capByDegree(
  nodes: GraphNode[],
  edges: GraphEdge[]
): { nodes: GraphNode[]; edges: GraphEdge[]; totalBeforeCap: number } {
  const totalBeforeCap = nodes.length;
  if (nodes.length <= MAX_RENDERED_NODES) return { nodes, edges, totalBeforeCap };

  const degree: Record<string, number> = {};
  for (const e of edges) {
    degree[e.source] = (degree[e.source] || 0) + 1;
    degree[e.target] = (degree[e.target] || 0) + 1;
  }
  const kept = [...nodes]
    .sort((a, b) => (degree[b.id] || 0) - (degree[a.id] || 0))
    .slice(0, MAX_RENDERED_NODES);
  const keptIds = new Set(kept.map((n) => n.id));
  const keptEdges = edges.filter((e) => keptIds.has(e.source) && keptIds.has(e.target));
  return { nodes: kept, edges: keptEdges, totalBeforeCap };
}

const W = 860;
const H = 560;

// Grid-based repulsion: only compares each node against others in its own
// and adjacent cells, instead of every pair. Turns the O(n^2) inner loop
// into roughly O(n) for a reasonably spread-out layout, which is what
// keeps a few-hundred-node function-level graph interactive instead of
// freezing the tab.
function applyRepulsion(sim: SimNode[], cellSize: number, maxDist: number, strength: number) {
  const buckets = new Map<string, SimNode[]>();
  const cellKey = (x: number, y: number) => `${Math.floor(x / cellSize)}:${Math.floor(y / cellSize)}`;
  for (const n of sim) {
    const k = cellKey(n.x, n.y);
    let arr = buckets.get(k);
    if (!arr) buckets.set(k, (arr = []));
    arr.push(n);
  }
  for (const n of sim) {
    const cx = Math.floor(n.x / cellSize);
    const cy = Math.floor(n.y / cellSize);
    for (let dx = -1; dx <= 1; dx++) {
      for (let dy = -1; dy <= 1; dy++) {
        const arr = buckets.get(`${cx + dx}:${cy + dy}`);
        if (!arr) continue;
        for (const other of arr) {
          if (other === n) continue;
          const ddx = n.x - other.x;
          const ddy = n.y - other.y;
          const dist = Math.sqrt(ddx * ddx + ddy * ddy) || 1;
          if (dist > maxDist) continue;
          const force = strength / (dist * dist);
          n.x += (ddx / dist) * force;
          n.y += (ddy / dist) * force;
        }
      }
    }
  }
}

function layout(nodes: GraphNode[], edges: GraphEdge[]): SimNode[] {
  const degree: Record<string, number> = {};
  for (const e of edges) {
    degree[e.source] = (degree[e.source] || 0) + 1;
    degree[e.target] = (degree[e.target] || 0) + 1;
  }

  const sim: SimNode[] = nodes.map((n, i) => {
    const angle = (i / Math.max(nodes.length, 1)) * Math.PI * 2;
    const r = 120 + (i % 3) * 40; // stagger the starting ring so large graphs don't start perfectly overlapped
    return {
      id: n.id,
      label: shortLabel(n),
      kind: n.kind,
      isTest: isTestPath(n),
      degree: degree[n.id] || 0,
      x: W / 2 + Math.cos(angle) * r + (Math.random() - 0.5) * 20,
      y: H / 2 + Math.sin(angle) * r + (Math.random() - 0.5) * 20,
    };
  });
  const byId: Record<string, SimNode> = {};
  sim.forEach((n) => (byId[n.id] = n));
  const links = edges
    .map((e) => ({ a: byId[e.source], b: byId[e.target] }))
    .filter((l) => l.a && l.b);

  const iterations = sim.length > 150 ? 220 : sim.length > 60 ? 260 : 320;
  for (let it = 0; it < iterations; it++) {
    applyRepulsion(sim, 40, 220, 900);

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
    for (const n of sim) {
      n.x += (W / 2 - n.x) * 0.004;
      n.y += (H / 2 - n.y) * 0.004;
      n.x = Math.max(24, Math.min(W - 24, n.x));
      n.y = Math.max(24, Math.min(H - 24, n.y));
    }
  }
  return sim;
}

interface ViewTransform {
  x: number;
  y: number;
  k: number;
}

export function CodeGraphView({ analysisId }: { analysisId: string }) {
  const [raw, setRaw] = useState<{ nodes: GraphNode[]; edges: GraphEdge[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showFunctions, setShowFunctions] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [positions, setPositions] = useState<SimNode[] | null>(null);
  const [transform, setTransform] = useState<ViewTransform>({ x: 0, y: 0, k: 1 });
  const dragId = useRef<string | null>(null);
  const panStart = useRef<{ px: number; py: number; tx: number; ty: number } | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);

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

  const capped = useMemo(() => {
    if (!raw) return null;
    const base = showFunctions ? raw : toFileLevel(raw.nodes, raw.edges);
    return capByDegree(base.nodes, base.edges);
  }, [raw, showFunctions]);

  useEffect(() => {
    if (!capped) return;
    setSelected(null);
    setTransform({ x: 0, y: 0, k: 1 });
    setPositions(layout(capped.nodes, capped.edges));
  }, [capped]);

  // Native (non-React-synthetic) wheel listener so preventDefault reliably
  // stops page scroll — React 17+ attaches wheel listeners as passive by
  // default, which silently ignores e.preventDefault() in a JSX onWheel.
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const onWheel = (ev: WheelEvent) => {
      ev.preventDefault();
      const rect = el.getBoundingClientRect();
      const vx = ((ev.clientX - rect.left) / rect.width) * W;
      const vy = ((ev.clientY - rect.top) / rect.height) * H;
      setTransform((t) => {
        const k = Math.min(5, Math.max(0.25, t.k * (ev.deltaY < 0 ? 1.12 : 0.89)));
        const worldX = (vx - t.x) / t.k;
        const worldY = (vy - t.y) / t.k;
        return { k, x: vx - worldX * k, y: vy - worldY * k };
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const nodesById = useMemo(() => {
    const m: Record<string, SimNode> = {};
    (positions || []).forEach((n) => (m[n.id] = n));
    return m;
  }, [positions]);

  const connected = useMemo(() => {
    const focus = selected || hovered;
    if (!focus || !capped) return null;
    const s = new Set<string>([focus]);
    for (const e of capped.edges) {
      if (e.source === focus) s.add(e.target);
      if (e.target === focus) s.add(e.source);
    }
    return s;
  }, [selected, hovered, capped]);

  const toViewBox = (ev: { clientX: number; clientY: number }) => {
    const rect = svgRef.current!.getBoundingClientRect();
    return {
      vx: ((ev.clientX - rect.left) / rect.width) * W,
      vy: ((ev.clientY - rect.top) / rect.height) * H,
    };
  };

  const onPointerMove = (ev: React.PointerEvent<SVGSVGElement>) => {
    if (dragId.current && positions) {
      const { vx, vy } = toViewBox(ev);
      const x = (vx - transform.x) / transform.k;
      const y = (vy - transform.y) / transform.k;
      setPositions((prev) => (prev || []).map((n) => (n.id === dragId.current ? { ...n, x, y } : n)));
    } else if (panStart.current) {
      const { vx, vy } = toViewBox(ev);
      const { px, py, tx, ty } = panStart.current;
      setTransform((t) => ({ ...t, x: tx + (vx - px), y: ty + (vy - py) }));
    }
  };

  const endInteraction = () => {
    dragId.current = null;
    panStart.current = null;
  };

  if (error) return <div style={{ color: "#c24a3f", fontSize: 13 }}>{error}</div>;
  if (!raw || !capped || !positions) {
    return <div style={{ color: "var(--dim)", fontSize: 13 }}>Loading dependency graph...</div>;
  }

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 16, marginBottom: 10, fontSize: 11 }}>
        <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", color: "var(--dim)" }}>
          <input type="checkbox" checked={showFunctions} onChange={(e) => setShowFunctions(e.target.checked)} />
          Show functions
        </label>
        <span style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--dim)" }}>
          <span style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--tag-fact)", display: "inline-block" }} />
          file
        </span>
        <span style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--dim)" }}>
          <span style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--tag-recommendation)", display: "inline-block" }} />
          test file
        </span>
        {showFunctions && (
          <span style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--dim)" }}>
            <span style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--tag-inference)", display: "inline-block" }} />
            function
          </span>
        )}
        <span style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--dim)" }}>
          <span style={{ width: 14, height: 1, background: "var(--tag-evidence)", display: "inline-block" }} />
          imports
        </span>
        <span style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--dim)" }}>
          <span style={{ width: 14, height: 1, background: "var(--dim)", display: "inline-block" }} />
          calls
        </span>
        <button
          onClick={() => setTransform({ x: 0, y: 0, k: 1 })}
          style={{
            marginLeft: "auto", fontSize: 11, background: "transparent", color: "var(--dim)",
            border: "1px solid var(--hairline)", borderRadius: 3, padding: "2px 8px", cursor: "pointer",
          }}
        >
          Reset view
        </button>
      </div>

      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        style={{ width: "100%", height: 560, background: "var(--panel)", border: "1px solid var(--hairline)", borderRadius: 4, cursor: panStart.current ? "grabbing" : "grab" }}
        onPointerDown={(e) => {
          if (e.target === e.currentTarget) {
            const { vx, vy } = toViewBox(e);
            panStart.current = { px: vx, py: vy, tx: transform.x, ty: transform.y };
          }
        }}
        onPointerMove={onPointerMove}
        onPointerUp={endInteraction}
        onPointerLeave={endInteraction}
        onClick={(e) => {
          if (e.target === e.currentTarget) setSelected(null);
        }}
      >
        <defs>
          {Object.entries(EDGE_COLOR).map(([kind, color]) => (
            <marker
              key={kind}
              id={`arrow-${kind}`}
              viewBox="0 0 10 10"
              refX="9" refY="5"
              markerWidth="6" markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill={color} />
            </marker>
          ))}
        </defs>
        <g transform={`translate(${transform.x},${transform.y}) scale(${transform.k})`}>
          {capped.edges.map((e, i) => {
            const a = nodesById[e.source];
            const b = nodesById[e.target];
            if (!a || !b) return null;
            const dim = connected && !(connected.has(e.source) && connected.has(e.target));
            const active = connected && connected.has(e.source) && connected.has(e.target);
            // shorten the line so the arrowhead sits at the target node's
            // rim instead of under it
            const dx = b.x - a.x, dy = b.y - a.y;
            const len = Math.sqrt(dx * dx + dy * dy) || 1;
            const pad = e.kind === "FILE_CONTAINS_FUNCTION" ? 0 : 7;
            const ex = b.x - (dx / len) * pad, ey = b.y - (dy / len) * pad;
            return (
              <line
                key={i}
                x1={a.x} y1={a.y} x2={ex} y2={ey}
                stroke={EDGE_COLOR[e.kind] || "var(--hairline)"}
                strokeWidth={((e.kind === "IMPORTS" ? 1.1 : 0.7) * (active ? 1.8 : 1)) / transform.k}
                opacity={dim ? 0.06 : e.kind === "FILE_CONTAINS_FUNCTION" ? 0.25 : active ? 0.9 : 0.5}
                markerEnd={e.kind === "FILE_CONTAINS_FUNCTION" ? undefined : `url(#arrow-${e.kind})`}
                style={{ transition: "opacity 150ms ease, stroke-width 150ms ease" }}
              />
            );
          })}
          {positions.map((n) => {
            const dim = connected && !connected.has(n.id);
            const focused = selected === n.id || hovered === n.id;
            const showLabel = focused || (!connected && n.kind === "file") || (connected != null && connected.has(n.id));
            const color = n.kind === "function" ? NODE_COLOR.function : n.isTest ? NODE_COLOR.fileTest : NODE_COLOR.file;
            const r = (n.kind === "function" ? 3.5 : 5 + Math.min(4, n.degree * 0.4)) / transform.k;
            return (
              <g
                key={n.id}
                transform={`translate(${n.x},${n.y})`}
                style={{ cursor: "pointer", color }}
                opacity={dim ? 0.2 : 1}
                onPointerDown={(e) => {
                  e.stopPropagation();
                  dragId.current = n.id;
                }}
                onPointerEnter={() => setHovered(n.id)}
                onPointerLeave={() => setHovered((h) => (h === n.id ? null : h))}
                onClick={(e) => {
                  e.stopPropagation();
                  setSelected(n.id === selected ? null : n.id);
                }}
              >
                {selected === n.id && (
                  <circle
                    r={r + 5 / transform.k}
                    fill="none"
                    stroke={color}
                    strokeWidth={1 / transform.k}
                    className="status-pulse"
                  />
                )}
                <circle
                  r={r}
                  fill={color}
                  stroke="var(--panel)"
                  strokeWidth={1.5 / transform.k}
                  style={{
                    transition: "r 150ms ease, filter 150ms ease",
                    filter: focused ? "drop-shadow(0 0 5px currentColor)" : "drop-shadow(0 0 1.5px currentColor)",
                  }}
                />
                {showLabel && (
                  <text
                    x={9 / transform.k} y={3 / transform.k}
                    fontFamily="var(--font-mono)"
                    fontSize={10 / transform.k}
                    fill={focused ? "var(--paper)" : "var(--dim)"}
                  >
                    {n.label}
                  </text>
                )}
              </g>
            );
          })}
        </g>
      </svg>
      <div style={{ fontSize: 11, color: "var(--dim)", marginTop: 6 }}>
        {capped.nodes.length} nodes, {capped.edges.length} edges
        {capped.totalBeforeCap > capped.nodes.length &&
          ` (showing the ${capped.nodes.length} most-connected of ${capped.totalBeforeCap})`}
        . Scroll to zoom, drag the background to pan, drag a node to reposition, click a node to trace its
        connections.
      </div>
    </div>
  );
}
