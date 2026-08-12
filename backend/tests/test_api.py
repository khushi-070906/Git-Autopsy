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
