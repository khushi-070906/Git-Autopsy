import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app

client = TestClient(app)


def setup_module(_module):
    Base.metadata.create_all(bind=engine)


def test_rejects_invalid_repo_url():
    resp = client.post("/api/analyze", json={"repo_url": "https://evil.com/x/y"})
    assert resp.status_code == 400


def test_404_for_unknown_analysis():
    resp = client.get("/api/analysis/does-not-exist")
    assert resp.status_code == 404


def test_full_pipeline_via_api(demo_repo):
    """Points the cloner at the local demo repo instead of hitting the
    network, so this test is deterministic and offline, then drives the
    job through the real background pipeline and API endpoints."""
    with patch("app.pipeline.cloner.clone_repository", return_value=demo_repo):
        with patch("app.pipeline.cloner.cleanup_workdir", return_value=None):
            resp = client.post("/api/analyze", json={"repo_url": "https://github.com/demo/demo"})
            assert resp.status_code == 200
            analysis_id = resp.json()["id"]

            # TestClient runs BackgroundTasks synchronously after the response,
            # so the job should already be done by the time we poll.
            for _ in range(20):
                status_resp = client.get(f"/api/analysis/{analysis_id}")
                if status_resp.json()["status"] in ("completed", "failed"):
                    break
                time.sleep(0.1)

            data = status_resp.json()
            assert data["status"] == "completed", data.get("error")
            assert data["result"]["top_root_cause"] is not None
            assert "Upgrade dependency" in data["result"]["top_root_cause"]["message"]

            commits_resp = client.get(f"/api/analysis/{analysis_id}/commits")
            assert commits_resp.status_code == 200
            assert len(commits_resp.json()) == 3

            graph_resp = client.get(f"/api/analysis/{analysis_id}/graph")
            assert graph_resp.status_code == 200
            assert len(graph_resp.json()["nodes"]) > 0

            regressions_resp = client.get(f"/api/analysis/{analysis_id}/regressions")
            assert regressions_resp.status_code == 200
            assert regressions_resp.json()["status"] == "insufficient_data"

            # --- code-graph: file/function dependency graph -----------------
            code_graph_resp = client.get(f"/api/analysis/{analysis_id}/code-graph")
            assert code_graph_resp.status_code == 200
            code_graph = code_graph_resp.json()
            node_kinds = {n["kind"] for n in code_graph["nodes"]}
            assert node_kinds <= {"file", "function"}
            edge_kinds = {e["kind"] for e in code_graph["edges"]}
            assert edge_kinds <= {"FILE_CONTAINS_FUNCTION", "IMPORTS", "CALLS"}
            # tests/test_model_loader.py imports load_model/tokenize by
            # calling them (FUNCTION_USED_BY_TEST, not tracked here) — but
            # FILE_CONTAINS_FUNCTION for model_loader.py's own functions
            # must be present.
            fn_names = {n["name"] for n in code_graph["nodes"] if n["kind"] == "function"}
            assert {"tokenize", "load_model"} <= fn_names

            # --- commit diff -------------------------------------------------
            commits = commits_resp.json()
            upgrade_commit = next(c for c in commits if "Upgrade dependency" in c["message"])
            target_sha = upgrade_commit["sha"]
            with patch("app.main.clone_repository", return_value=demo_repo):
                with patch("app.main.cleanup_workdir", return_value=None):
                    diff_resp = client.get(f"/api/analysis/{analysis_id}/commit/{target_sha}/diff")
            assert diff_resp.status_code == 200
            diff_body = diff_resp.json()
            assert diff_body["sha"] == target_sha
            assert "fast_tokenizer" in diff_body["diff"]

            # root commit (no parent) must also produce a valid diff, not an error
            root_sha = next(c["sha"] for c in commits if "Initial working version" in c["message"])
            with patch("app.main.clone_repository", return_value=demo_repo):
                with patch("app.main.cleanup_workdir", return_value=None):
                    root_diff_resp = client.get(f"/api/analysis/{analysis_id}/commit/{root_sha}/diff")
            assert root_diff_resp.status_code == 200
            assert "unable to generate diff" not in root_diff_resp.json()["diff"]
            assert "def tokenize" in root_diff_resp.json()["diff"]

            bad_sha_resp = client.get(f"/api/analysis/{analysis_id}/commit/deadbeef/diff")
            assert bad_sha_resp.status_code == 400
