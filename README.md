# AUTOPSY

**Forensic analysis for software.**

AUTOPSY analyzes a public GitHub repository's git history, source code,
dependencies, and tests, and produces an evidence-based report answering:
what likely went wrong, when it started, which commit is the strongest
suspected cause, which files/functions were affected, what evidence
supports each conclusion, and what to investigate next.

Every conclusion is traced back to a concrete fact. The system explicitly
distinguishes **FACT**, **EVIDENCE**, **INFERENCE**, and **RECOMMENDATION** —
it never presents a guess as a certainty.

---

## 60-second demo (no setup, no network)

```bash
python run_demo.py
```

This builds a small local git repository with a deliberately injected
regression (a dependency upgrade that silently swaps in a different
tokenizer implementation), runs the full analysis pipeline against it, and
prints a case report. It finishes in well under a second and requires no
GitHub account, API key, or network access — it's the fastest way to see
the reasoning engine work.

To see the same repository through the real HTTP API + web dashboard, see
[Local development](#local-development) below, or read
[`demo_repo/`](./demo_repo) directly.

---

## Architecture

```
┌──────────────┐      POST /api/analyze       ┌───────────────────┐
│   Frontend    │ ───────────────────────────▶ │     FastAPI        │
│ React + Vite  │                               │     backend        │
│               │ ◀─── GET /api/analysis/{id} ─│                     │
└──────────────┘        (polling)               └─────────┬──────────┘
                                                             │ BackgroundTasks
                                                             ▼
                                        ┌────────────────────────────────┐
                                        │  Phase 1  Clone (isolated,      │
                                        │           validated, size-capped)│
                                        │  Phase 2  Git history + AST     │
                                        │           static analysis       │
                                        │  Phase 3  Evidence Graph         │
                                        │           (NetworkX)             │
                                        │  Phase 4  WHY analysis           │
                                        │           (deterministic scoring)│
                                        │  Phase 5  Dependency graph        │
                                        │  Phase 7  Regression detection    │
                                        │  Phase 8  Optional AI explanation │
                                        │           layer (structured input)│
                                        └────────────────┬──────────────────┘
                                                          ▼
                                                   SQLite (results)
```

**The Evidence Graph is the foundation.** Every fact the pipeline discovers
becomes a typed node (`commit`, `file`, `function`, `dependency`, `test`,
`author`); every relationship becomes a typed edge
(`COMMIT_CHANGED_FILE`, `COMMIT_CHANGED_DEPENDENCY`, `FILE_CONTAINS_FUNCTION`,
`FUNCTION_USED_BY_TEST`, `FILE_DEPENDS_ON_PACKAGE`, `COMMIT_AUTHORED_BY`).
The WHY analysis engine (`app/analysis/why_analysis.py`) reasons over this
graph with transparent, additive scoring rules — it does not re-derive facts
and does not delegate reasoning to an LLM. AUTOPSY never sends an entire
repository to an LLM and asks "what's wrong" — the optional AI layer only
ever receives the already-computed structured evidence, to phrase it in
plain English.

### Project structure

```
autopsy/
├── backend/
│   ├── app/
│   │   ├── main.py                 API endpoints
│   │   ├── pipeline.py             orchestrates all phases as one background job
│   │   ├── database.py             SQLite (SQLAlchemy)
│   │   ├── security.py             URL validation, path traversal / injection prevention
│   │   └── analysis/
│   │       ├── cloner.py           Phase 1 — safe isolated cloning
│   │       ├── git_history.py      Phase 1/2 — commit + diff extraction
│   │       ├── detect.py           Phase 2 — language / dependency / test-framework detection
│   │       ├── static_python.py    Phase 2 — AST-based function/import extraction (no exec)
│   │       ├── dependency_parser.py Phase 5 — manifest parsing
│   │       ├── evidence_graph.py   Phase 3 — the Evidence Graph (NetworkX)
│   │       ├── why_analysis.py     Phase 4 — deterministic causal scoring
│   │       ├── regression_detection.py Phase 7 — regression flags + health score
│   │       └── ai_layer.py         Phase 8 — optional LLM explanation layer
│   ├── tests/                      pytest suite (35 tests)
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── App.tsx                 landing page + investigation dashboard
│   │   ├── EvidenceGraphView.tsx   interactive evidence graph (click a node → see evidence)
│   │   ├── EvidenceTag.tsx         FACT/EVIDENCE/INFERENCE/RECOMMENDATION tag component
│   │   └── api.ts                  typed API client
│   └── vercel.json
├── demo_repo/                      the 3-commit demo repository, checked in
└── run_demo.py                     standalone offline demo runner
```

---

## Local development

### Backend

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

The API is now at `http://localhost:8000`. Interactive docs at
`http://localhost:8000/docs`.

To enable the optional AI explanation layer:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Without it, AUTOPSY still runs the full deterministic pipeline and falls
back to a template-based explanation — the core analysis is identical
either way.

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`. Vite proxies `/api/*` to `http://localhost:8000`
in dev (see `vite.config.ts`).

### Running the test suite

```bash
cd backend
pytest -q
```

35 tests cover: URL validation and path-traversal/injection rejection, git
history extraction, AST-based static analysis (including proof that
malicious/erroring Python is parsed, never executed), Evidence Graph
construction, WHY-analysis scoring and evidence-category discipline,
dependency manifest parsing, and a full API integration test that drives
the real background pipeline end-to-end and asserts the injected demo
regression is correctly surfaced as the top suspect.

---

## API

| Method | Path | Description |
|---|---|---|
| POST | `/api/analyze` | Start analysis. Body: `{"repo_url": "https://github.com/user/repo"}`. Returns `{id, status}`. |
| GET | `/api/analysis/{id}` | Full status + result once completed. |
| GET | `/api/analysis/{id}/commits` | Commit list. |
| GET | `/api/analysis/{id}/graph` | Evidence Graph as `{nodes, edges}` JSON. |
| GET | `/api/analysis/{id}/regressions` | Regression detection output. |
| GET | `/api/analysis/{id}/dependencies` | Dependency manifests + graph dependency nodes. |
| GET | `/api/analysis/{id}/history` | Commits + function inventory. |
| GET | `/health` | Liveness check. |

Status values: `queued → cloning → indexing → building_graph → analyzing →
completed` (or `failed`, with `error` populated).

Analysis runs as a FastAPI `BackgroundTask`, so the POST returns
immediately and the frontend polls for status.

---

## Deployment

**Backend** — any container host (Fly.io, Render, Railway, a small VM).

```bash
cd backend
docker build -t autopsy-backend .
docker run -p 8000:8000 -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY autopsy-backend
```

Notes:
- Uses SQLite by default — fine for a single-instance deployment. Swap
  `DATABASE_URL` in `app/database.py` for Postgres before scaling to
  multiple instances.
- Set `allow_origins` in `app/main.py`'s CORS middleware to your deployed
  frontend origin instead of `*` before going to production.

**Frontend** — Vercel.

```bash
cd frontend
vercel deploy
```

Edit `vercel.json` to point the `/api/*` rewrite at your deployed backend
host, or set `VITE_API_PROXY_TARGET` and adjust `api.ts` to use an absolute
URL if you prefer not to rewrite.

---

## Security

This is the part of the spec treated as non-negotiable, so it's worth
stating plainly what is and isn't done:

- **Only `https://github.com/<owner>/<repo>` URLs are accepted.** Validated
  by regex plus an explicit character blocklist before anything touches the
  filesystem or a subprocess (`app/security.py::validate_github_url`).
- **No shell interpolation.** Cloning uses `subprocess.run` with a fixed
  argv list (`shell=False`) — the validated URL is passed as a single
  argument, never interpolated into a shell string.
- **No arbitrary code execution.** Source analysis is 100% static: `git
  log`/`git diff` metadata and Python's `ast` module. Repository code is
  parsed, never imported or run. (Verified by
  `tests/test_static_python.py::test_never_executes_code`.)
- **No `pip install` / `npm install` of repository dependencies.**
  Dependency manifests are parsed as text, never installed.
- **Isolated, disposable clone directories** — every job gets a fresh
  `tempfile`-based directory under a UUID, cleaned up after the job
  finishes (success or failure).
- **Size and timeout limits** — clone is capped at a configurable byte
  limit (`MAX_REPO_SIZE_BYTES`, default 500 MB) and a hard subprocess
  timeout (`CLONE_TIMEOUT_SECONDS`, default 120s).
- **Path traversal prevention** — any path built from repository-controlled
  data (file names from diffs, etc.) should go through
  `app/security.py::safe_join`, which rejects any result that escapes the
  intended base directory.

### Known limitations (stated plainly, not hidden)

1. **No sandboxed test execution.** The spec explicitly requires this to be
   a clearly separated, optional feature if implemented at all — it is
   **not implemented** in this V1. As a direct consequence, regression
   detection cannot know which tests actually failed on which commit; it
   says `"Insufficient historical test evidence"` and falls back to static
   heuristics (dependency changes, large diffs, risk-signaling commit
   messages), clearly labeled as unconfirmed.
2. **Static analysis (AST) is Python-only in this V1.** Language detection
   covers more languages; deep function/call extraction currently does not.
3. **WHY-analysis confidence scores are heuristic, not statistically
   calibrated** against a labeled dataset of real regressions — they are
   deliberately capped below 100% and every contributing signal is shown,
   but they should be read as "how many independent risk signals fired,"
   not a calibrated probability.
4. **The "used by test" edge is heuristic** (name-based call matching
   within test files), not true call-graph/import resolution — it can
   under- or over-associate a function with a test.
5. **CORS is wide open (`*`) by default** for local development
   convenience — must be restricted before a production deployment.

### Next three technically valuable improvements

1. **Isolated, opt-in test execution** in a fully sandboxed, network-disabled
   container (gVisor/Firecracker-style isolation) to convert "suspicious
   changes" into confirmed pass→fail regressions — the single highest-value
   addition, since it replaces heuristic confidence with observed evidence.
2. **Tree-sitter-based multi-language static analysis** to bring the
   function/call-graph extraction that Python gets from `ast` to
   JavaScript/TypeScript, Go, and Rust, widening the Evidence Graph's
   function/test edges beyond Python repos.
3. **True import/call-graph resolution** (replacing the current name-based
   heuristic) so `FUNCTION_USED_BY_TEST` and cross-file call edges are
   structurally verified rather than inferred from identifier matching —
   this would materially sharpen WHY-analysis precision on larger repos.
