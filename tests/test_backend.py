import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Conflict, LostLease, Store
from app.main import create_app
from app.worker import Worker
from tests.test_agent import sample


@pytest.fixture
def settings(tmp_path):
    return Settings(database=tmp_path / "test.sqlite3", worker_enabled=False)


def test_idempotency_and_concurrent_queue_claim(settings):
    store = Store(settings.database)
    run_id, created = store.create(sample(), "same-key")
    assert created
    assert store.create(sample(), "same-key") == (run_id, False)
    with pytest.raises(Conflict):
        store.create(sample("missing"), "same-key")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda owner: store.claim(owner, 30, 3), ["a", "b"]))
    assert sum(r is not None for r in results) == 1


def test_recovery_fences_old_worker_and_preserves_checkpoint(settings):
    store = Store(settings.database)
    rid, _ = store.create(sample(), "recover-key")
    store.claim("old", 30, 3)
    store.checkpoint(rid, "old", {"progress": 42}, "tool", {})
    with store.connect() as db:
        db.execute("UPDATE runs SET lease_until=? WHERE id=?", (time.time() - 1, rid))
    recovered = Store(settings.database).claim("new", 30, 3)
    assert recovered["checkpoint"] == {"progress": 42}
    assert recovered["attempts"] == 2
    with pytest.raises(LostLease):
        store.checkpoint(rid, "old", {}, "tool", {})
    assert store.get(rid)["checkpoint"] == {"progress": 42}


def test_recovery_attempt_limit(settings):
    store = Store(settings.database)
    rid, _ = store.create(sample(), "attempt-key")
    store.claim("old", -1, 1)
    assert store.claim("new", 30, 1) is None
    assert store.get(rid)["status"] == "failed"


def test_api_roundtrip_clarifications_and_export(settings):
    app = create_app(settings)
    store = app.state.store
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/health").status_code == 200
        headers = {"Idempotency-Key": "roundtrip-key"}
        response = client.post("/api/runs", json=sample("missing"), headers=headers)
        assert response.status_code == 201
        rid = response.json()["id"]
        assert client.post("/api/runs", json=sample("missing"), headers=headers).status_code == 200
        worker = Worker(store, settings)
        asyncio.run(worker.process(store.claim(worker.owner, 30, 3)))
        assert client.get(f"/api/runs/{rid}").json()["status"] == "needs_clarification"
        response = client.post(
            f"/api/runs/{rid}/clarifications",
            headers={"Idempotency-Key": "clarify-key"},
            json={
                "answers": {
                    "rights": "Требуется проверить договор автора",
                    "placement": "общая карточка",
                    "event_date": "20.10.2026",
                }
            },
        )
        assert response.status_code == 201
        child = response.json()["id"]
        asyncio.run(worker.process(store.claim(worker.owner, 30, 3)))
        assert client.get(f"/api/runs/{child}").json()["status"] == "completed"
        assert client.get(f"/api/runs/{child}").json()["parent_id"] == rid
        assert client.get(f"/api/runs/{rid}").json()["status"] == "needs_clarification"
        export = client.get(f"/api/runs/{child}/export")
        assert export.json()["events"]
        assert "checkpoint" not in export.json()["run"]
        assert client.post(f"/api/runs/{child}/retry").status_code == 409
        assert client.get("/api/runs/not-found").status_code == 404


def test_invalid_input_cross_origin_and_size(settings):
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/runs", json=sample()).status_code == 422
        assert (
            client.post(
                "/api/runs",
                json=sample(),
                headers={"Origin": "https://untrusted.example", "Idempotency-Key": "attack-key"},
            ).status_code
            == 403
        )
        assert client.post("/api/runs", content=b"x" * 300001).status_code == 413
        payload = sample()
        payload["documents"] *= 7
        assert (
            client.post(
                "/api/runs", json=payload, headers={"Idempotency-Key": "invalid-key"}
            ).status_code
            == 422
        )
        assert client.get("/api/config").json()["yandex_ready"] is False


def test_yandex_requires_configuration_and_consent(settings):
    payload = sample()
    payload["mode"] = "yandex"
    with TestClient(create_app(settings)) as client:
        assert (
            client.post(
                "/api/runs", json=payload, headers={"Idempotency-Key": "yandex-key"}
            ).status_code
            == 503
        )
    settings.api_key = "fake"
    settings.folder_id = "folder"
    settings.enable_yandex = True
    with TestClient(create_app(settings)) as client:
        assert (
            client.post(
                "/api/runs", json=payload, headers={"Idempotency-Key": "yandex-key"}
            ).status_code
            == 422
        )


def test_real_background_worker_completes(settings):
    settings.worker_enabled = True
    settings.poll_seconds = 0.01
    with TestClient(create_app(settings)) as client:
        rid = client.post(
            "/api/runs", json=sample(), headers={"Idempotency-Key": "background-key"}
        ).json()["id"]
        for _ in range(100):
            run = client.get(f"/api/runs/{rid}").json()
            if run["status"] == "completed":
                break
            time.sleep(0.02)
        assert run["status"] == "completed"
        assert client.get("/api/health").json()["worker"]


def test_role_options_are_real_html_options(settings):
    class Options(HTMLParser):
        def __init__(self):
            super().__init__()
            self.values = []

        def handle_starttag(self, tag, attrs):
            if tag == "option":
                self.values.append(dict(attrs).get("value"))

    with TestClient(create_app(settings)) as client:
        parser = Options()
        parser.feed(client.get("/").text)
        assert {"claimant", "respondent", "unknown"}.issubset(parser.values)


def test_public_config_describes_current_research_without_credentials(settings):
    from app.contracts import CONTRACT_VERSION

    settings.parser_api_key = "sensitive-parser-value"
    settings.gigachat_auth_key = "sensitive-model-value"
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/config")
        value = response.json()
        assert value["contract_version"] == CONTRACT_VERSION
        assert value["source_integration"] == "parser_api_kad_ras"
        assert value["tool_research_enabled"] and value["semantic_critic_enabled"]
        assert value["originals_retained"] and value["limits"]["research_acts"] == 8
        assert "sensitive-" not in response.text
