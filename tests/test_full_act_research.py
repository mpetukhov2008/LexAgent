import asyncio
import base64
import hashlib

import httpx
import pytest

from app.agent import AgentError
from app.agents.tool_research import ResearchAgent
from app.config import Settings
from app.contracts import ResearchRequest, case_key
from app.sources import ParserClient
from tests.test_roles_v2 import Script


def test_middle_page_and_linked_instances():
    quote = "Ответчик возражал против исковых требований полностью"
    pages = [{"page": n, "text": quote if n == 7 else f"Страница {n} документа."}
             for n in range(1, 15)]
    acts = [{"id": "a", "number": "А70-1/2026", "pages": pages},
            {"id": "b", "number": "А70-1/2026", "pages": []}]
    events = []
    provider = Script([
        ("list_case_acts", {}),
        ("get_case_history", {"case_key": case_key("А70-1/2026")}),
        ("read_act_page", {"act_id": "a", "page": 7}),
        ("record_evidence", {"items": [{"act_id": "a", "page": 7, "quote": quote,
                                         "assertion_type": "party_argument"}]}),
        ("finish_research", {}),
    ])
    result = asyncio.run(ResearchAgent(provider, lambda *x: events.append(x)).research(
        ResearchRequest(intake={}, acts=acts)))
    assert len(result.evidence) == 1
    item = result.evidence[0]
    assert item.page == 7 and item.act_id == "a" and item.assertion_type == "party_argument"
    assert not result.history_complete
    assert any("истории" in x for x in result.warnings)
    assert any("акты без доступного текста" in x for x in result.warnings)
    assert [e[1]["tool"] for e in events] == [
        "list_case_acts", "get_case_history", "read_act_page", "record_evidence", "finish_research"]


def test_card_document_and_text_are_separate_cache_and_scan_limit(tmp_path, monkeypatch):
    requests = []
    url = "https://kad.arbitr.ru/document.pdf"

    def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith("details_by_number"):
            return httpx.Response(200, json={"done": 1, "Cases": [{
                "CaseNumber": "А70-1/2026", "CaseId": "test", "CaseInstances": [
                    {"File": {"URL": url}}, {"File": {"URL": url}}]}]})
        return httpx.Response(200, json={"done": 1,
                                        "pdfContent": base64.b64encode(b"%PDF-test").decode()})


    async def run():
        client = ParserClient(Settings(parser_api_key="dummy"), httpx.MockTransport(handler),
                              tmp_path, extractor=lambda raw: [{"page": 1, "text": "Текст"}])
        card = await client.get_case_card("А70-1/2026")
        acts = client.list_case_acts(card)
        assert len(acts) == 1
        document = await client.download_document(acts[0])
        assert document == b"%PDF-test"
        assert await client.read_act(acts[0]) == [{"page": 1, "text": "Текст"}]
        assert acts[0]["document_sha256"] == hashlib.sha256(document).hexdigest()
        assert acts[0]["text_origin"] == "pdf_text"
        await client.read_act(acts[0])
        assert client.calls == 2
        client.extractor = lambda raw: [{"page": 1, "text": ""}]
        with pytest.raises(AgentError, match="OCR"):
            await client.read_act(acts[0])

    asyncio.run(run())
    assert len(requests) == 2


def test_access_error_never_becomes_empty_case_list(tmp_path):
    def handler(request):
        return httpx.Response(403, json={"error_code": "40301"})

    async def run():
        client = ParserClient(Settings(parser_api_key="dummy"), httpx.MockTransport(handler),
                              tmp_path)
        with pytest.raises(AgentError, match="отклонён"):
            await client.case_by_number("А70-1/2026")

    asyncio.run(run())


def test_same_registered_act_saved_twice_has_one_library_row(tmp_path):
    from app.db import Store
    from app.library import Library

    store = Store(tmp_path / "test.sqlite")
    library = Library(store)
    act = {"id": "source-act-1", "number": "А70-1/2026", "document_sha256": "a" * 64,
           "pages": [{"page": 1, "text": "текст"}]}
    library.save(act)
    library.save(act)
    assert len(library.recent()) == 1
    with store.connect() as db:
        row = db.execute("SELECT payload FROM judicial_acts WHERE id=?", (act["id"],)).fetchone()
    assert '"document_sha256"' in row["payload"]


def test_historical_corpus_manifest_matches_committed_text():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    manifest = json.loads((root / "samples/corpus/manifest.json").read_text(encoding="utf-8"))
    # Git's Windows checkout can use CRLF; the manifest hashes UTF-8 with LF.
    report_bytes = (root / manifest["source_report"]).read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(report_bytes).hexdigest() == manifest["source_report_sha256"]
    report = json.loads(report_bytes)
    candidates = {}

    def walk(value):
        if isinstance(value, dict):
            if value.get("pages") and value.get("file_url") and value.get("id"):
                candidates[value["id"]] = value
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(report)
    assert len(manifest["acts"]) == 3
    for entry in manifest["acts"]:
        pages = candidates[entry["act_id"]]["pages"]
        canonical = json.dumps(pages, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode()
        assert hashlib.sha256(canonical).hexdigest() == entry["pages_sha256"]
        assert len(pages) == entry["page_count"]
        assert entry["pdf_sha256"] is None and entry["legal_review"] == "not_reviewed"
