import asyncio
import base64
import logging
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import AgentError
from app.agents.critic import CriticAgent
from app.config import Settings
from app.contracts import ReviewRequest, normalize_evidence, registry
from app.db import Store
from app.documents import OriginalStore
from app.library import Library
from app.main import create_app
from app.pdf_worker import ocr_ready
from app.sources import ParserClient


def test_originals_are_permanent_deduplicated_and_integrity_checked(tmp_path):
    store = OriginalStore(tmp_path)
    digest = store.put(b"%PDF-test")
    assert store.put(b"%PDF-test") == digest
    assert len(list(tmp_path.rglob("*.pdf"))) == 1
    assert store.get(digest).read_bytes() == b"%PDF-test"
    store.path(digest).write_bytes(b"changed")
    with pytest.raises(AgentError, match="целостность"):
        store.get(digest)
    with pytest.raises(AgentError):
        store.get("../outside")


def test_identical_pdf_from_two_urls_has_one_library_identity(tmp_path):
    library = Library(Store(tmp_path / "library.sqlite"))
    first = {
        "id": "first-url",
        "number": "А70-1/2026",
        "document_sha256": "a" * 64,
        "pages": [{"page": 1, "text": "Фотографии товара"}],
    }
    second = {**first, "id": "other-url"}
    library.save(first)
    library.save(second)
    assert second["id"] == first["id"] and len(library.recent()) == 1
    with pytest.raises(AgentError, match="разными номерами"):
        library.save({**first, "number": "А70-2/2026"})
    updated = {
        **first,
        "id": "first-url",
        "source_act_id": "first-url",
        "document_sha256": "b" * 64,
        "pages": [{"page": 1, "text": "Изменённый PDF"}],
    }
    library.save(updated)
    assert updated["id"] != first["id"] and len(library.recent()) == 2
    assert library.get("first-url")["document_sha256"] == "b" * 64
    assert library.get(first["id"])["document_sha256"] == "a" * 64


def test_429_retry_pagination_and_same_case_siblings_are_preserved(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="private-key")
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "done": 1,
                "pages": 2,
                "count": 2,
                "items": [
                    {
                        "CaseNumber": "А70-1/2026",
                        "FileUrl": f"https://kad.arbitr.ru/{page}.pdf",
                        "InstanceLevel": page,
                        "Court": "Учебный суд",
                    }
                ],
            },
        )

    client = ParserClient(
        Settings(parser_api_key="private-key", source_backoff=0),
        httpx.MockTransport(handler),
        tmp_path,
    )
    acts, coverage = asyncio.run(client.search("фотографии"))
    assert len(acts) == 2 and {a["instance"] for a in acts} == {"first", "appeal"}
    assert coverage["pagination_complete"] and coverage["fetched_pages"] == 2
    assert client.calls == len(requests) == 3
    assert all("private-key" not in p.read_text(encoding="utf-8")
               for p in tmp_path.glob("*.json"))


def test_source_budget_survives_new_client_and_reserves_before_network(tmp_path):
    budget, reservations, calls = {}, [], []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectTimeout("private-key")

    settings = Settings(parser_api_key="dummy", source_max_calls=2, source_backoff=0)
    for _ in range(2):
        client = ParserClient(settings, httpx.MockTransport(handler), tmp_path)
        client.set_budget(budget, lambda: reservations.append(budget["source_calls"]))
        with pytest.raises(AgentError):
            asyncio.run(client.case_by_number("А70-1/2026"))
    assert len(calls) == 2 and reservations == [1, 2] and budget["source_calls"] == 2


def test_http_diagnostic_logs_redact_query_credentials(tmp_path, caplog):
    client = ParserClient(
        Settings(parser_api_key="private-source-key"),
        httpx.MockTransport(
            lambda r: httpx.Response(200, json={"done": 1, "pages": 1, "items": []})
        ),
        tmp_path,
    )
    with caplog.at_level(logging.INFO, logger="httpx"):
        asyncio.run(client.search("фотографии"))
    assert "private-source-key" not in caplog.text
    assert "REDACTED" in caplog.text


def test_wrong_case_number_in_downloaded_text_blocks_evidence(tmp_path):
    client = ParserClient(
        Settings(parser_api_key="dummy"),
        httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"done": 1, "pdfContent": base64.b64encode(b"%PDF-test").decode()}
            )
        ),
        tmp_path,
        extractor=lambda raw: [
            {"page": 1, "text": "Дело А70-999/2025. Суд установил обстоятельства."}
        ],
    )
    act = {"id": "wrong", "number": "А70-1/2026", "file_url": "https://kad.arbitr.ru/a.pdf"}
    act["pages"] = asyncio.run(client.read_act(act))
    evidence, _ = normalize_evidence(
        [{"act_id": "wrong", "page": 1, "quote": act["pages"][0]["text"]}], [act]
    )
    result = asyncio.run(
        CriticAgent().review(
            ReviewRequest(
                intake={},
                comparisons=[],
                evidence=evidence,
                coverage={"history_complete": True},
                acts=registry([act])[1],
            )
        )
    )
    assert act["extraction_quality"]["case_number_status"] == "mismatch"
    assert result.verdict == "insufficient" and result.issues[0].blocking


def test_full_published_history_includes_events_instances_and_cancellation(tmp_path):
    def handler(request):
        if "details_by_number" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "Success": 1,
                    "Cases": [
                        {
                            "CaseNumber": "А70-1/2026",
                            "CaseId": "example",
                            "CaseInstances": [
                                {
                                    "Name": name,
                                    "Court": {"Name": "Учебный суд"},
                                    "File": {"URL": f"https://kad.arbitr.ru/{n}.pdf"},
                                    "InstanceEvents": [
                                        {
                                            "File": f"https://kad.arbitr.ru/event-{n}.pdf",
                                            "EventContentTypeName": "Об отмене решения",
                                        }
                                    ],
                                }
                                for n, name in enumerate(
                                    ["Первая инстанция", "Апелляция", "Кассация", "Верховный суд"]
                                )
                            ],
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"done": 1, "pages": 1, "items": []})

    client = ParserClient(Settings(parser_api_key="dummy"), httpx.MockTransport(handler), tmp_path)
    acts, history = asyncio.run(client.get_case_history("А70-1/2026"))
    assert len(acts) == 8 and history["history_complete"]
    assert not history["legal_finality_verified"] and history["checked_at"]
    assert history["links"] and any("отмене" in a["appeal_status"] for a in acts)


def test_history_partial_ras_failure_cannot_be_complete(tmp_path):
    client = ParserClient(
        Settings(parser_api_key="dummy", source_attempts=1),
        httpx.MockTransport(lambda r: httpx.Response(403)),
        tmp_path,
    )
    acts, history = asyncio.run(client.get_case_history("А70-1/2026"))
    assert not acts and not history["history_complete"] and history["warnings"]


def test_original_api_uses_registered_digest_and_retains_ocr_failure(tmp_path):
    settings = Settings(
        database=tmp_path / "db", document_dir=tmp_path / "originals", worker_enabled=False
    )
    digest = OriginalStore(settings.document_dir).put(b"%PDF-test")
    with TestClient(create_app(settings)) as client:
        response = client.get(f"/api/documents/{digest}/original")
        assert response.status_code == 200 and response.content == b"%PDF-test"
        assert "sandbox" in response.headers["content-security-policy"]
        assert client.get("/api/documents/not-a-digest/original").status_code == 404


def test_real_scan_ocr_preserves_original_and_page_provenance(tmp_path):
    if not ocr_ready():
        if os.getenv("REQUIRE_OCR") == "1":
            pytest.fail("Real Tesseract rus+eng required in this CI job")
        pytest.skip("Tesseract rus+eng not installed")
    raw = (Path(__file__).parent / "fixtures/ocr-rus-scan.pdf").read_bytes()
    client = ParserClient(
        Settings(parser_api_key="dummy"),
        httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"done": 1, "pdfContent": base64.b64encode(raw).decode()}
            )
        ),
        tmp_path,
    )
    act = {"id": "scan", "number": "А70-1/2026", "file_url": "https://kad.arbitr.ru/scan.pdf"}
    pages = asyncio.run(client.read_act(act))
    assert "фотограф" in pages[0]["text"].casefold()
    assert act["text_origin"] == "ocr" and act["extraction_quality"]["ocr_used"]
    assert act["page_quality"][0]["requires_review"]
    assert client.originals.get(act["document_sha256"]).read_bytes() == raw
