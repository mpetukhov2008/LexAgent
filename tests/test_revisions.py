import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import LostLease
from app.main import create_app
from tests.test_agent import sample


def test_revision_retains_original_and_cancel_resume_retains_checkpoint(tmp_path):
    app = create_app(Settings(database=tmp_path / "db", worker_enabled=False))
    store = app.state.store
    with TestClient(app) as client:
        rid = client.post("/api/runs", json=sample(), headers={
            "Idempotency-Key": "original-key"
        }).json()["id"]
        store.claim("worker", 30, 3)
        store.checkpoint(rid, "worker", {"intake": {"steps": 3}}, "tool", {})
        assert client.post(f"/api/runs/{rid}/cancel").json()["status"] == "cancelled"
        with pytest.raises(LostLease):
            store.checkpoint(rid, "worker", {}, "tool", {})
        assert client.post(f"/api/runs/{rid}/resume").status_code == 200
        assert store.get(rid)["checkpoint"] == {"intake": {"steps": 3}}
        corrected = {**sample(), "title": "Исправленная ситуация", "revision_note": "Уточнил ввод"}
        child = client.post(f"/api/runs/{rid}/revisions", json=corrected, headers={
            "Idempotency-Key": "revision-key"
        }).json()["id"]
        assert store.get(child)["parent_id"] == rid
        assert store.get(child)["payload"]["revision_note"] == "Уточнил ввод"
        assert store.get(rid)["payload"]["title"] == sample()["title"]
