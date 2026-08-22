const BASE = "/api";
export interface EvidenceItem {
  kind: "FACT" | "EVIDENCE" | "INFERENCE" | "RECOMMENDATION";
  text: string;
}
export interface Suspect {
  commit_sha: string;
  short_sha: string;
  message: string;
  author: string;
  date: string;
  confidence: number;
  summary: string;
  affected_files: string[];
  affected_functions: string[];
  evidence: EvidenceItem[];
}
export interface Commit {
  sha: string;
  short_sha: string;
  author: string;
  date: string;
  message: string;
  files_changed: string[];
}
export interface GraphNode {
  id: string;
  kind: string;
  [key: string]: unknown;
}
export interface GraphEdge {
  source: string;
  target: string;
  kind: string;
  [key: string]: unknown;
}
export interface AnalysisResult {
  id: string;
  repo_url: string;
  language: { dominant_language: string; file_counts_by_language: Record<string, number> };
  dependency_files: string[];
  test_frameworks: string[];
  health: {
    repository_health_score: number;
    risk_level: "LOW" | "MEDIUM" | "HIGH";
    likely_regressions: number;
    dependency_risks: number;
    suspicious_changes: number;
    commits_analyzed: number;
    files_analyzed: number;
  };
  top_root_cause: (Suspect & { ai_explanation: string }) | null;
  suspects: Suspect[];
  regressions: { status: string; message: string; note: string };
  commit_count: number;
  commits: Commit[];
  graph: { nodes: GraphNode[]; edges: GraphEdge[] };
}
export interface AnalysisStatus {
  id: string;
  repo_url: string;
  status: string;
  error: string | null;
  result: AnalysisResult | null;
}
export async function startAnalysis(repoUrl: string): Promise<{ id: string; status: string }> {
  const resp = await fetch(`${BASE}/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo_url: repoUrl }),
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(body.detail || "Failed to start analysis.");
  }
  return resp.json();
}
export async function getAnalysis(id: string): Promise<AnalysisStatus> {
  const resp = await fetch(`${BASE}/analysis/${id}`);
  if (!resp.ok) throw new Error("Failed to fetch analysis status.");
  return resp.json();
}

// --- Counterfactual replay ---------------------------------------------
//
// Mirrors app/analysis/counterfactual.py's CounterfactualResult shape as
// serialized by main.py's _run_counterfactual_job. `result` is null until
// the job's status is "completed" or "failed" — poll getCounterfactualJob
// until status is no longer "queued"/"running".

export interface CounterfactualResultPayload {
  commit_sha: string;
  short_sha: string;
  framework: string;
  removes_failure: boolean;
  baseline_failing_tests: string[];
  without_commit_failing_tests: string[];
  baseline_timed_out: boolean;
  without_commit_timed_out: boolean;
  baseline_raw_output: string;
  without_commit_raw_output: string;
}

export interface CounterfactualJob {
  status: "queued" | "running" | "completed" | "failed";
  error: string | null;
  result: CounterfactualResultPayload | null;
}

export async function startCounterfactual(
  analysisId: string,
  commitSha: string
): Promise<{ job_id: string; status: string }> {
  const resp = await fetch(`${BASE}/analysis/${analysisId}/counterfactual`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ commit_sha: commitSha }),
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(body.detail || "Failed to start counterfactual replay.");
  }
  return resp.json();
}

export async function getCounterfactualJob(jobId: string): Promise<CounterfactualJob> {
  const resp = await fetch(`${BASE}/counterfactual/${jobId}`);
  if (!resp.ok) throw new Error("Failed to fetch counterfactual job status.");
  return resp.json();
}

// --- Code dependency graph -----------------------------------------------
//
// Mirrors main.py's GET /api/analysis/{id}/code-graph: file and function
// nodes plus FILE_CONTAINS_FUNCTION/IMPORTS/CALLS edges, filtered out of
// the full evidence graph and de-duplicated (one arrow per caller/callee
// pair, not one per call site).

export interface CodeGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  cycles: string[][];
}

export async function getCodeGraph(analysisId: string): Promise<CodeGraph> {
  const resp = await fetch(`${BASE}/analysis/${analysisId}/code-graph`);
  if (!resp.ok) throw new Error("Failed to fetch code dependency graph.");
  return resp.json();
}

// --- Commit diff -----------------------------------------------------------
//
// Mirrors main.py's GET /api/analysis/{id}/commit/{sha}/diff. Re-clones the
// repo on the backend to read it (the repo isn't kept around after the
// analysis pipeline finishes), so this can be slower than other endpoints
// and is rate-limited server-side the same way analysis starts are.

export interface CommitDiff {
  sha: string;
  diff: string;
}

export async function getCommitDiff(analysisId: string, sha: string): Promise<CommitDiff> {
  const resp = await fetch(`${BASE}/analysis/${analysisId}/commit/${sha}/diff`);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(body.detail || "Failed to fetch commit diff.");
  }
  return resp.json();
}
