"""Boundary regressions for the participant-2 integration; no external API calls."""

import asyncio
import base64
import io
import json

import httpx
import pytest
from pypdf import PdfWriter

from app.agent import AgentError
from app.agents.tool_research import ResearchAgent
from app.config import Settings
from app.contracts import ResearchRequest
from app.sources import ParserClient, court_url, extract_pages
from tests.test_roles_v2 import Script


@pytest.mark.parametrize("url", [
    "https://kad.arbitr.ru:8443/file.pdf",
    "https://kad.arbitr.ru:invalid/file.pdf",
    "https://:password@kad.arbitr.ru/file.pdf",
    "https://kad.arbitr.ru/file.pdf#https://evil.test/",
    "https://kad.arbitr.ru/\nfile.pdf",
    " https://kad.arbitr.ru/file.pdf",
    "https://kad.arbitr.ru\\@evil.test/file.pdf",
    "https://127.0.0.1/file.pdf",
    "https://kad.arbitr.ru.evil.test/file.pdf",
    "http://kad.arbitr.ru/file.pdf",
])
def test_document_addresses_reject_ambiguous_authority_ports_and_controls(url, tmp_path):
    calls = []
    client = ParserClient(Settings(parser_api_key="dummy"),
                          httpx.MockTransport(lambda r: calls.append(r)), tmp_path)
    assert not court_url(url)
    with pytest.raises(AgentError, match="адрес"):
        asyncio.run(client.download_document({"file_url": url}))
    assert not calls and client.calls == 0


@pytest.mark.parametrize("url", [
    "https://kad.arbitr.ru/Document/Pdf/акт.pdf",
    "https://ras.arbitr.ru:443/document.pdf?download=1",
])
def test_normal_source_addresses_remain_supported(url):
    assert court_url(url)


@pytest.mark.parametrize("instances", ["invalid", [None], [{"File": "invalid"}],
                                        [{"File": {"URL": "https://kad.arbitr.ru/a"},
                                          "Court": "invalid"}]])
def test_malformed_upstream_instances_are_source_errors(instances, tmp_path):
    client = ParserClient(Settings(), cache_dir=tmp_path)
    with pytest.raises(AgentError, match="карточку"):
        client.list_case_acts({"number": "А70-1/2026", "rows": [{
            "CaseNumber": "А70-1/2026", "CaseInstances": instances}]})


def test_instance_date_is_not_used_as_document_date(tmp_path):
    client = ParserClient(Settings(), cache_dir=tmp_path)
    acts = client.list_case_acts({"number": "А70-1/2026", "rows": [{
        "CaseNumber": "А70-1/2026", "CaseInstances": [{
            "Date": "instance-event-date", "File": {"URL": "https://kad.arbitr.ru/a"}}]}]})
    assert acts[0]["date"] == ""


@pytest.mark.parametrize("pages", [None, {}, "invalid", [None], ["invalid"],
                                    [{"page": True, "text": "Текст"}],
                                    [{"page": 1, "text": None}]])
def test_malformed_research_pages_fail_with_controlled_error(pages):
    request = ResearchRequest(intake={}, acts=[{"id": "a", "number": "А70-1/2026",
                                               "pages": pages}])
    with pytest.raises(AgentError):
        asyncio.run(ResearchAgent(Script([])).research(request))


@pytest.mark.parametrize("failure", ["403", "429", "timeout", "redirect"])
def test_source_failure_is_not_empty_results_and_does_not_leak_key(failure, tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private-key", request=request)
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.test/private-key"})
        return httpx.Response(int(failure), json={"error_code": "40301" if failure == "403"
                                                else "unknown", "body": "private-key"})

    client = ParserClient(Settings(parser_api_key="private-key", source_attempts=1),
                          httpx.MockTransport(handler),
                          tmp_path)
    with pytest.raises(AgentError) as exc:
        asyncio.run(client.case_by_number("А70-1/2026"))
    assert "private-key" not in str(exc.value)
    assert len(calls) == client.calls == 1
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("content", ["not base64!", base64.b64encode(b"not pdf").decode(),
                                      base64.b64encode(b"%PDF-broken").decode()])
def test_invalid_documents_never_become_empty_pages(content, tmp_path):
    client = ParserClient(Settings(parser_api_key="dummy"), httpx.MockTransport(
        lambda r: httpx.Response(200, json={"done": 1, "pdfContent": content})), tmp_path)
    with pytest.raises(AgentError, match="PDF"):
        asyncio.run(client.read_act({"file_url": "https://kad.arbitr.ru/a"}))


def test_partial_and_truncated_text_is_not_marked_complete(tmp_path, monkeypatch):
    client = ParserClient(Settings(parser_api_key="dummy"), httpx.MockTransport(
        lambda r: httpx.Response(200, json={"done": 1, "pdfContent":
                                          base64.b64encode(b"%PDF-test").decode()})), tmp_path)
    client.extractor = lambda raw: [
        {"page": 1, "text": "x" * 20000}, {"page": 2, "text": ""}]
    act = {"file_url": "https://kad.arbitr.ru/a"}
    asyncio.run(client.read_act(act))
    quality = act["extraction_quality"]
    assert quality["status"] == "partial" and quality["quality_assessed"] is False
    assert quality["empty_pages"] == [2] and quality["possibly_truncated_pages"] == [1]


def test_evidence_limit_and_event_privacy():
    quotes = [f"Синтетическая цитата номер {n:02d}." for n in range(31)]
    steps = [("list_case_acts", {}), ("read_act_page", {"act_id": "a", "page": 1})]
    for start in range(0, 30, 10):
        steps.append(("record_evidence", {"items": [
            {"act_id": "a", "page": 1, "quote": q} for q in quotes[start:start + 10]]}))
    steps += [("record_evidence", {"items": [{"act_id": "a", "page": 1,
                                               "quote": quotes[-1]}]}), ("finish_research", {})]
    events, script = [], Script(steps)
    result = asyncio.run(ResearchAgent(script, lambda *e: events.append(e)).research(
        ResearchRequest(intake={}, acts=[{"id": "a", "number": "А70-1/2026", "pages": [
            {"page": 1, "text": " ".join(quotes)}]}])))
    assert len(result.evidence) == 30 and script.errors.count(True) == 1
    assert not any(q in json.dumps(events, ensure_ascii=False) for q in quotes)


def test_source_budget_is_enforced_before_ninth_request(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"done": 1, "Cases": []})

    async def run():
        client = ParserClient(Settings(parser_api_key="dummy", source_max_calls=8),
                              httpx.MockTransport(handler),
                              tmp_path)
        for _ in range(8):
            await client.request("arbitr_api/details_by_number", {}, fresh=True)
        with pytest.raises(AgentError, match="лимит 8"):
            await client.request("arbitr_api/details_by_number", {}, fresh=True)

    asyncio.run(run())
    assert len(calls) == 8


def test_oversized_source_response_is_rejected(tmp_path):
    client = ParserClient(Settings(parser_api_key="dummy"), httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"x" * 16_000_001)), tmp_path)
    with pytest.raises(AgentError, match="16 МБ"):
        asyncio.run(client.case_by_number("А70-1/2026"))


def test_oversized_decoded_pdf_is_rejected(tmp_path):
    content = base64.b64encode(b"%PDF" + b"x" * 10_000_000).decode()
    client = ParserClient(Settings(parser_api_key="dummy"), httpx.MockTransport(
        lambda r: httpx.Response(200, json={"done": 1, "pdfContent": content})), tmp_path)
    with pytest.raises(AgentError, match="PDF"):
        asyncio.run(client.download_document({"file_url": "https://kad.arbitr.ru/a"}))


def test_real_blank_pdf_requires_ocr(tmp_path):
    buffer = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(buffer)
    content = base64.b64encode(buffer.getvalue()).decode()
    client = ParserClient(Settings(parser_api_key="dummy"), httpx.MockTransport(
        lambda r: httpx.Response(200, json={"done": 1, "pdfContent": content})), tmp_path)
    with pytest.raises(AgentError, match="OCR"):
        asyncio.run(client.read_act({"file_url": "https://kad.arbitr.ru/a"}))


def test_page_limit_is_enforced_with_real_pdf():
    buffer = io.BytesIO()
    writer = PdfWriter()
    for _ in range(151):
        writer.add_blank_page(width=100, height=100)
    writer.write(buffer)
    with pytest.raises(ValueError, match="PDF extraction failed"):
        extract_pages(buffer.getvalue())
