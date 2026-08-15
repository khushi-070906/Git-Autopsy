import { useEffect, useRef, useState } from "react";
import { AnalysisResult, getAnalysis, startAnalysis } from "./api";
import { EvidenceTag } from "./EvidenceTag";
import { EvidenceGraphView } from "./EvidenceGraphView";
import { VitalsMonitor } from "./VitalsMonitor";
import { ErrorBoundary } from "./ErrorBoundary";

const STATUS_LABELS: Record<string, string> = {
  queued: "in the queue…",
  cloning: "pulling the repo",
  indexing: "reading the receipts",
  building_graph: "connecting the dots",
  analyzing: "naming a suspect",
  completed: "case closed",
  failed: "couldn't crack this one",
};

const RISK_COLOR: Record<string, string> = {
  LOW: "var(--risk-low)",
  MEDIUM: "var(--risk-medium)",
  HIGH: "var(--risk-high)",
};

export default function App() {
  const [repoUrl, setRepoUrl] = useState("");
  const [analysisId, setAnalysisId] = useState<string | null>(null);
  const [status, setStatus] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AnalysisResult | null>(null);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    if (!analysisId) return;
    pollRef.current = window.setInterval(async () => {
      try {
        const data = await getAnalysis(analysisId);
        setStatus(data.status);
        if (data.status === "completed") {
          if (!data.result) {
            setError("Analysis completed but returned no data. Please try again.");
          } else {
            setResult(data.result);
          }
          if (pollRef.current) window.clearInterval(pollRef.current);
        } else if (data.status === "failed") {
          setError(data.error || "Analysis failed.");
          if (pollRef.current) window.clearInterval(pollRef.current);
        }
      } catch (e) {
        // transient network hiccup — keep polling
      }
    }, 1200);
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [analysisId]);

  async function onAnalyze() {
    setError(null);
    setResult(null);
    try {
      const { id, status } = await startAnalysis(repoUrl.trim());
      setAnalysisId(id);
      setStatus(status);
    } catch (e: any) {
      setError(e.message || "Failed to start analysis.");
    }
  }

  async function onRefresh() {
    if (!analysisId) return;
    try {
      const data = await getAnalysis(analysisId);
      if (data.result) setResult(data.result);
    } catch {
      // transient — leave current result showing rather than clearing it
    }
  }

  return (
    <div style={{ minHeight: "100%", padding: "0 0 80px" }}>
      <Header />

      {!result && (
        <Landing
          repoUrl={repoUrl}
          setRepoUrl={setRepoUrl}
          onAnalyze={onAnalyze}
          status={analysisId ? status : ""}
          error={error}
        />
      )}

      {result && (
        <ErrorBoundary>
          <Dashboard
            result={result}
            onReset={() => { setResult(null); setAnalysisId(null); setRepoUrl(""); }}
            onRefresh={onRefresh}
          />
        </ErrorBoundary>
      )}
    </div>
  );
}

function Header() {
  return (
    <div
      className="case-header"
      style={{
        borderBottom: "1px solid var(--hairline)",
        padding: "16px 32px",
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        flexWrap: "wrap",
        gap: 10,
        position: "sticky",
        top: 0,
        background: "var(--bg)",
        zIndex: 10,
      }}
    >
      <div style={{ fontFamily: "var(--font-display)", fontSize: 18, letterSpacing: "-0.01em", color: "var(--paper)" }}>
        git<span style={{ color: "var(--finding)" }}>-</span>autopsy
      </div>
      <div className="mono-label">forensic analysis for your repo</div>
    </div>
  );
}

function Landing({
  repoUrl, setRepoUrl, onAnalyze, status, error,
}: {
  repoUrl: string; setRepoUrl: (v: string) => void; onAnalyze: () => void; status: string; error: string | null;
}) {
  const busy = !!status && status !== "completed" && status !== "failed";
  return (
    <div style={{ maxWidth: 720, margin: "10vh auto 0", padding: "0 24px", textAlign: "center" }}>
      <div className="mono-label" style={{ marginBottom: 18 }}>INTAKE</div>
      <h1 style={{ fontFamily: "var(--font-display)", fontSize: 36, fontWeight: 500, margin: "0 0 16px", lineHeight: 1.2, letterSpacing: "-0.01em" }}>
        find out what<br />broke your code
      </h1>
      <p style={{ color: "var(--dim)", fontSize: 15, margin: "0 0 36px" }}>
        drop in a public repo. we'll read the commit history, trace the evidence,
        and name the most likely cause — nothing asserted without a fact behind it.
      </p>

      <div className="intake-row" style={{ display: "flex", gap: 10, marginBottom: 18 }}>
        <input
          value={repoUrl}
          onChange={(e) => setRepoUrl(e.target.value)}
          placeholder="https://github.com/user/repository"
          onKeyDown={(e) => e.key === "Enter" && !busy && repoUrl && onAnalyze()}
          autoFocus
          style={{
            flex: 1,
            minWidth: 0,
            background: "var(--panel)",
            border: "1px solid var(--hairline)",
            borderRadius: 4,
            padding: "12px 14px",
            color: "var(--paper)",
            fontFamily: "var(--font-mono)",
            fontSize: 14,
          }}
        />
        <button
          className="btn-primary"
          onClick={onAnalyze}
          disabled={busy || !repoUrl}
          style={{
            background: "transparent",
            color: busy ? "var(--dim)" : "var(--glow)",
            borderRadius: 4,
            padding: "12px 22px",
            fontWeight: 600,
            letterSpacing: "0.02em",
            fontSize: 13,
            opacity: busy || !repoUrl ? 0.6 : 1,
            whiteSpace: "nowrap",
          }}
        >
          {busy ? "RUNNING…" : "RUN THE EXAM"}
        </button>
      </div>

      {status && (
        <div style={{ marginBottom: 14 }}>
          <div className="sprockets" />
          <VitalsMonitor
            mode={busy ? "scanning" : status === "failed" ? "flatline" : "stable"}
            label={STATUS_LABELS[status] || status}
          />
        </div>
      )}
      {error && (
        <div
          style={{
            color: "var(--found)",
            fontSize: 13,
            marginTop: 14,
            padding: "10px 14px",
            border: "1px solid var(--found)",
            borderRadius: 4,
            textAlign: "left",
            background: "rgba(209, 73, 61, 0.08)",
          }}
        >
          {error}
        </div>
      )}

      <div style={{ marginTop: 56, textAlign: "left", border: "1px solid var(--hairline)", borderRadius: 4, padding: 18 }}>
        <div className="mono-label" style={{ marginBottom: 8 }}>Examination protocol</div>
        <ul style={{ margin: 0, paddingLeft: 18, color: "var(--dim)", fontSize: 13, lineHeight: 1.9 }}>
          <li>Static analysis only — repository code is never executed</li>
          <li>Isolated chain of custody: a fresh clone, a size cap, a hard timeout</li>
          <li>Every finding traced to a specific commit, file, or dependency</li>
        </ul>
      </div>
    </div>
  );
}

function Dashboard({ result, onReset, onRefresh }: { result: AnalysisResult; onReset: () => void; onRefresh: () => void }) {
  const h = result.health ?? {
    repository_health_score: 0,
    risk_level: "LOW",
    likely_regressions: 0,
    dependency_risks: 0,
    suspicious_changes: 0,
  };
  const suspects = result.suspects ?? [];
  const graph = result.graph ?? { nodes: [], edges: [] };
  const testFrameworks = result.test_frameworks ?? [];
  const dependencyFiles = result.dependency_files ?? [];
  const language = result.language ?? { dominant_language: "Unknown" };
  const regressions = result.regressions ?? { message: "No data available.", note: "" };

  const hasFinding = !!result.top_root_cause;

  return (
    <div style={{ maxWidth: 1080, margin: "0 auto", padding: "32px 24px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20, flexWrap: "wrap", gap: 12 }}>
        <div>
          <div className="mono-label">CASE #{(result.id ?? "unknown").slice(0, 8).toUpperCase()} — SUBJECT</div>
          <div className="toe-tag" style={{ marginTop: 6, fontSize: 13 }}>{result.repo_url}</div>
        </div>
        <button
          className="btn-ghost"
          onClick={onReset}
          style={{ background: "none", border: "1px solid var(--hairline)", color: "var(--dim)", borderRadius: 4, padding: "8px 14px", fontSize: 12, flexShrink: 0 }}
        >
          NEW CASE
        </button>
      </div>

      <div style={{ marginBottom: 24 }}>
        <div className="sprockets" />
        <VitalsMonitor
          mode={hasFinding ? "flatline" : "stable"}
          label={hasFinding ? "Cause of failure determined" : "No abnormalities detected"}
          markerText={hasFinding ? `Time of failure — ${result.top_root_cause!.short_sha}` : undefined}
        />
      </div>

      <div className="stat-grid" style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 12, marginBottom: 28 }}>
        <StatCard label="Vital Signs Score" value={`${h.repository_health_score} / 100`} />
        <StatCard label="Triage Level" value={h.risk_level} color={RISK_COLOR[h.risk_level]} />
        <StatCard label="Likely Regressions" value={String(h.likely_regressions)} />
        <StatCard label="Dependency Risks" value={String(h.dependency_risks)} />
        <StatCard label="Suspicious Changes" value={String(h.suspicious_changes)} />
      </div>

      {result.top_root_cause && (
        <Section title="CAUSE OF FAILURE">
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 14, flexWrap: "wrap", gap: 12 }}>
            <div>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: 22, color: "var(--found)" }}>
                {result.top_root_cause.short_sha}
              </div>
              <div style={{ color: "var(--dim)", fontSize: 13 }}>{result.top_root_cause.message}</div>
            </div>
            <ConfidenceRing value={result.top_root_cause.confidence ?? 0} />
          </div>
          <p style={{ fontSize: 14, color: "var(--paper)", marginBottom: 16 }}>{result.top_root_cause.ai_explanation}</p>
          <div>
            {(result.top_root_cause.evidence ?? []).map((e, i) => (
              <EvidenceTag item={e} key={i} />
            ))}
          </div>
        </Section>
      )}

      <Section title={`DIFFERENTIAL DIAGNOSIS (${suspects.length})`}>
        {suspects.length === 0 && (
          <div style={{ color: "var(--dim)", fontSize: 13 }}>No commits crossed the diagnostic confidence threshold.</div>
        )}
        {suspects.map((s) => (
          <details key={s.commit_sha} className="suspect-row" style={{ marginBottom: 10, border: "1px solid var(--hairline)", borderRadius: 4, padding: "10px 14px" }}>
            <summary style={{ cursor: "pointer", display: "flex", justifyContent: "space-between" }}>
              <span>
                <span style={{ color: "var(--found)", fontFamily: "var(--font-mono)" }}>{s.short_sha}</span>{" — "}
                {s.message}
              </span>
              <span style={{ color: "var(--dim)", fontFamily: "var(--font-mono)" }}>{Math.round((s.confidence ?? 0) * 100)}%</span>
            </summary>
            <div style={{ marginTop: 10 }}>
              {(s.evidence ?? []).map((e, i) => <EvidenceTag item={e} key={i} />)}
            </div>
          </details>
        ))}
      </Section>

      <Section title="SECONDARY FINDINGS">
        <div style={{ fontSize: 13, color: "var(--dim)", marginBottom: 6 }}>{regressions.message}</div>
        <div style={{ fontSize: 12, color: "var(--dim)" }}>{regressions.note}</div>
      </Section>

      <Section title="SUBJECT PROFILE">
        <div className="profile-grid" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20, fontSize: 13 }}>
          <div>
            <div className="mono-label" style={{ marginBottom: 6 }}>Language</div>
            <div>{language.dominant_language}</div>
          </div>
          <div>
            <div className="mono-label" style={{ marginBottom: 6 }}>Test framework</div>
            <div>{testFrameworks.join(", ") || "None detected"}</div>
          </div>
          <div>
            <div className="mono-label" style={{ marginBottom: 6 }}>Dependency manifests</div>
            <div>{dependencyFiles.join(", ") || "None found"}</div>
          </div>
          <div>
            <div className="mono-label" style={{ marginBottom: 6 }}>Commits reviewed</div>
            <div>{result.commit_count ?? 0}</div>
          </div>
        </div>
      </Section>

      <Section title="SYSTEM ANATOMY">
        <EvidenceGraphView analysisId={result.id} nodes={graph.nodes} edges={graph.edges} onCounterfactualComplete={onRefresh} />
      </Section>
    </div>
  );
}

function ConfidenceRing({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const r = 32;
  const c = 2 * Math.PI * r;
  const offset = c - (pct / 100) * c;
  const color = pct >= 60 ? "var(--found)" : pct >= 35 ? "var(--amber)" : "var(--dim)";
  return (
    <div className="confidence-ring">
      <svg viewBox="0 0 76 76">
        <circle cx="38" cy="38" r={r} fill="none" stroke="var(--hairline)" strokeWidth="5" />
        <circle
          cx="38" cy="38" r={r} fill="none"
          stroke={color} strokeWidth="5" strokeLinecap="round"
          strokeDasharray={c} strokeDashoffset={offset}
          style={{ transition: "stroke-dashoffset 500ms ease" }}
        />
      </svg>
      <span className="ring-value" style={{ color }}>{pct}%</span>
    </div>
  );
}

function StatCard({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="stat-card" style={{ borderRadius: 4, padding: "14px 16px" }}>
      <div className="mono-label" style={{ marginBottom: 8 }}>{label}</div>
      <div className="stat-value" style={{ fontFamily: "var(--font-mono)", fontWeight: 600, fontSize: 21, color: color || "var(--paper)" }}>{value}</div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 28 }}>
      <div className="mono-label" style={{ marginBottom: 12, color: "var(--glow)" }}>{title}</div>
      <div data-panel style={{ background: "var(--panel)", border: "1px solid var(--hairline)", borderRadius: 4, padding: 20 }}>
        {children}
      </div>
    </div>
  );
}
