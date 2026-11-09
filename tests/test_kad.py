import asyncio
import base64

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import AgentError
from app.config import Settings
from app.main import create_app
from app.pipeline import clean_tool_text, evidence_catalog, resolve_evidence, verify_evidence
from app.sources import ParserClient


def test_model_envelope_debris_is_removed_without_rewriting_prose():
    assert clean_tool_text({"summary": 'Проверить договор.\\"\\n}'}) == {
        "summary": "Проверить договор."
    }
    assert clean_tool_text('Суд указал: "право доказано".') == 'Суд указал: "право доказано".'


def test_evidence_ids_preserve_exact_pdf_text_and_last_pages():
    cases = [
        {
            "id": "a",
            "pages": [
                {"page": i, "text": "Суд  установил\nобстоятельства дела " + str(i)}
                for i in range(1, 11)
            ],
        }
    ]
    catalog = evidence_catalog(cases)
    assert {v["page"] for v in catalog.values()} == {1, 2, 9, 10}
    key = next(iter(catalog))
    resolved, rejected = resolve_evidence(
        [
            {"fragment_id": key, "observation": "Обстоятельства"},
            {"fragment_id": key, "observation": "Дубль"},
            {"fragment_id": "invented", "observation": "Выдумка"},
        ],
        catalog,
    )
    assert len(resolved) == 1 and rejected == 1
    assert resolved[0]["quote"] == cases[0]["pages"][0]["text"]
    assert verify_evidence(resolved, cases) == (resolved, 0)


def test_kad_lookup_download_and_validation(tmp_path, monkeypatch):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("pdf_download"):
            return httpx.Response(
                200,
                json={
                    "Success": 1,
                    "pdfContent": base64.b64encode(b"%PDF-test").decode(),
                },
            )
        assert request.url.params["CaseNumber"] == "А70-7129/2024"
        return httpx.Response(
            200,
            json={
                "Success": 1,
                "Cases": [
                    {
                        "CaseNumber": "А70-7129/2024",
                        "CaseId": "id",
                        "CaseInstances": [
                            {
                                "Court": {"Name": "Суд"},
                                "File": {"URL": "https://kad.arbitr.ru/file.pdf"},
                            },
                            {"File": {"URL": "https://attacker.example/file.pdf"}},
                        ],
                    }
                ],
            },
        )

    client = ParserClient(Settings(parser_api_key="secret"), httpx.MockTransport(handler), tmp_path,
                          extractor=lambda _: [{"page": 1, "text": "Акт"}])

    async def run():
        cases, coverage = await client.search("A70-7129/2024")
        assert coverage["source"] == "kad" and len(cases) == 1
        assert await client.read_act(cases[0]) == [{"page": 1, "text": "Акт"}]
        with pytest.raises(AgentError):
            await client.case_by_number("invalid")

    asyncio.run(run())
    assert paths == ["/parser/arbitr_api/details_by_number", "/parser/arbitr_api/pdf_download"]


def test_diagnostics_bypass_cache_and_distinguish_services(tmp_path):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if "ras_arbitr_api" in request.url.path:
            return httpx.Response(403, json={"error_code": 40301, "error": "secret"})
        return httpx.Response(200, json={"Success": 1, "Cases": []})

    client = ParserClient(Settings(parser_api_key="secret"), httpx.MockTransport(handler), tmp_path)

    async def run():
        await client.diagnose()
        result = await client.diagnose()
        assert result["kad"]["ok"] and not result["ras"]["ok"]
        assert "40301" in result["ras"]["message"]
        assert "secret" not in str(result)

    asyncio.run(run())
    assert len(paths) == 4


def test_kad_import_api_saves_pages_and_reports_errors(tmp_path, monkeypatch):
    async def lookup(self, number):
        return [{"id": "real-id", "number": number, "pages": []}]

    async def read(self, case):
        return [{"page": 1, "text": "Фотографии товара на Ozon"}]

    monkeypatch.setattr(ParserClient, "case_by_number", lookup)
    monkeypatch.setattr(ParserClient, "read_act", read)
    with TestClient(create_app(Settings(database=tmp_path / "db", worker_enabled=False))) as client:
        result = client.post("/api/library/kad", params={"number": "А70-7129/2024"})
        assert result.status_code == 200
        assert result.json()["imported"][0]["pages"] == 1
        assert client.get("/api/library").json()[0]["number"] == "А70-7129/2024"

        async def fail(self, case):
            raise AgentError("В PDF нет текста")

        monkeypatch.setattr(ParserClient, "read_act", fail)
        data = client.post("/api/library/kad", params={"number": "А70-7129/2024"}).json()
        assert not data["imported"] and data["warnings"]


def test_kad_import_respects_document_limit_and_reports_remaining(tmp_path, monkeypatch):
    read_ids = []

    async def lookup(self, number):
        return [{"id": str(i), "number": number, "pages": []} for i in range(4)]

    async def read(self, case):
        read_ids.append(case["id"])
        return [{"page": 1, "text": "Учебный текст"}]

    monkeypatch.setattr(ParserClient, "case_by_number", lookup)
    monkeypatch.setattr(ParserClient, "read_act", read)
    settings = Settings(database=tmp_path / "db", worker_enabled=False, research_acts=2)
    with TestClient(create_app(settings)) as client:
        result = client.post("/api/library/kad", params={"number": "А70-1/2026"}).json()
    assert read_ids == ["0", "1"]
    assert len(result["imported"]) == 2 and result["warnings"]
