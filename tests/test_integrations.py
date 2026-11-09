import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import AgentError, IntakeAgent, ToolError
from app.config import Settings
from app.gigachat import GigaChatProvider
from app.library import ActImport, Library
from app.main import create_app
from app.pipeline import SearchPipeline, SearchPlan, role_agent, verify_evidence
from app.schemas import Intake
from app.sources import ParserClient, court_url
from tests.test_agent import sample


def test_gigachat_langchain_messages_schema_and_auth_cache():
    from langchain_core.messages import AIMessage, ToolMessage

    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Basic secret-key"
        return httpx.Response(
            200, json={"access_token": "private-token", "expires_at": (time.time() + 1800) * 1000}
        )

    class Model:
        def bind_tools(self, tools, tool_choice):
            assert "$ref" not in json.dumps(tools)
            assert "$defs" not in json.dumps(tools)
            assert tool_choice == "auto"
            return self

        async def ainvoke(self, messages, config):
            if len(messages) > 1:
                assert isinstance(messages[-1], ToolMessage)
                assert messages[-1].name == "list_documents"
                assert messages[-2].additional_kwargs["functions_state_id"] == "state-1"
            assert config["callbacks"] == []
            return AIMessage(
                content="",
                tool_calls=[{"id": "call-1", "name": "list_documents", "args": {}}],
                additional_kwargs={"functions_state_id": "state-1"},
                usage_metadata={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
            )

    async def run():
        provider = GigaChatProvider(
            Settings(gigachat_auth_key="secret-key"), httpx.MockTransport(handler), model=Model()
        )
        assert await provider.access_token() == await provider.access_token()
        messages = [{"role": "user", "content": "Начни"}]
        message, usage = await provider.choose(messages)
        assert usage["prompt_tokens"] == 5
        await provider.choose(
            messages + [message, {"role": "tool", "tool_call_id": "call-1", "content": "[]"}]
        )

    asyncio.run(run())
    assert len(requests) == 1


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_gigachat_errors_never_expose_secrets(status):
    provider = GigaChatProvider(
        Settings(gigachat_auth_key="secret-key"),
        httpx.MockTransport(
            lambda request: httpx.Response(status, text="secret-key private-token")
        ),
    )
    with pytest.raises(AgentError) as error:
        asyncio.run(provider.access_token())
    assert "secret-key" not in str(error.value)
    assert str(status) in str(error.value)


def test_gigachat_requires_consent(tmp_path):
    settings = Settings(database=tmp_path / "db", gigachat_auth_key="secret", worker_enabled=False)
    payload = sample()
    payload["mode"] = "gigachat"
    with TestClient(create_app(settings)) as client:
        assert (
            client.post(
                "/api/runs", json=payload, headers={"Idempotency-Key": "consent-test"}
            ).status_code
            == 422
        )
        payload["consent_to_cloud"] = True
        assert (
            client.post(
                "/api/runs", json=payload, headers={"Idempotency-Key": "consent-test"}
            ).status_code
            == 201
        )
        assert "secret" not in client.get("/api/config").text


@pytest.mark.parametrize(
    "url",
    [
        "http://kad.arbitr.ru/a",
        "https://evil.test/a",
        "https://kad.arbitr.ru.evil.test/a",
        "https://user@kad.arbitr.ru/a",
        "file:///etc/passwd",
    ],
)
def test_source_url_validation(url):
    assert not court_url(url)


def test_parser_success_deduplication_and_cache(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        row = {
            "CaseNumber": "А00-1/2026",
            "FileUrl": "https://kad.arbitr.ru/Document/Pdf/test.pdf",
            "CaseUrl": "https://kad.arbitr.ru/Card/test",
            "Court": "Учебный суд",
        }
        return httpx.Response(200, json={"done": 1, "items": [row, row], "count": 2, "pages": 1})

    source = ParserClient(
        Settings(parser_api_key="private-key"), httpx.MockTransport(handler), tmp_path
    )
    first, coverage = asyncio.run(source.search("фотографии"))
    second, _ = asyncio.run(source.search("фотографии"))
    assert len(first) == 1 and first == second
    assert len(calls) == 1 and coverage["fetched_pages"] == 1
    assert all("private-key" not in file.read_text(encoding="utf-8") for file in tmp_path.iterdir())


@pytest.mark.parametrize(
    "status,body",
    [(403, {"error": "secret-key"}), (200, {"done": 0}), (200, {"done": 1, "items": None})],
)
def test_parser_failure_is_not_empty_search(tmp_path, status, body):
    source = ParserClient(
        Settings(parser_api_key="secret-key"),
        httpx.MockTransport(lambda request: httpx.Response(status, json=body)),
        tmp_path,
    )
    with pytest.raises(AgentError) as error:
        asyncio.run(source.search("фотографии"))
    assert "secret-key" not in str(error.value)


def test_library_import_and_local_pipeline(tmp_path):
    settings = Settings(database=tmp_path / "db", worker_enabled=False)
    app = create_app(settings)
    library = Library(app.state.store)
    value = ActImport(
        number="УЧЕБНЫЙ ПРИМЕР",
        court="Синтетический пример, не судебный акт",
        url="https://kad.arbitr.ru/Card/example",
        text=(
            "Учебный текст: фотографии товара на Ozon. "
            "Этот текст используется только для проверки интерфейса "
            "и не является судебной практикой."
        ),
    )
    library.import_act(value)
    library.import_act(value)
    assert len(library.recent()) == 1
    assert library.search("Ozon фотографии")

    async def run():
        payload = Intake.model_validate(sample()).model_dump()
        intake = await IntakeAgent(payload, settings).run(lambda *args: None)
        output = await SearchPipeline(settings, library=library).run(
            payload, intake, lambda *args: None
        )
        assert len(output["cases"]) == 1
        assert output["comparisons"][0]["relevance"] == "unknown"
        assert output["critique"]["sufficient"] is False
        assert output["status"] == "partial"

    asyncio.run(run())


def test_invalid_citations_are_rejected():
    cases = [
        {"id": "a", "pages": [{"page": 1, "text": "Суд установил факт размещения фотографий."}]}
    ]
    evidence = [
        {"case_id": "a", "page": 1, "quote": "факт размещения фотографий"},
        {"case_id": "a", "page": 2, "quote": "факт размещения фотографий"},
        {"case_id": "b", "page": 1, "quote": "факт размещения фотографий"},
        {"case_id": "a", "page": 1, "quote": "Иск полностью удовлетворён"},
    ]
    verified, rejected = verify_evidence(evidence, cases)
    assert verified == evidence[:1] and rejected == 3


def test_online_agent_cannot_finish_with_unprocessed_fields():
    payload = sample()
    payload["mode"] = "gigachat"
    agent = IntakeAgent(payload, Settings())
    for source in agent.sources:
        agent.execute("read_document", {"source_id": source})
    with pytest.raises(ToolError, match="обработки полей"):
        agent.execute("finish", {"reason": "Описание прочитано"})
    assert not agent.state["finished"]


def test_available_tools_follow_prerequisites():
    agent = IntakeAgent(sample(), Settings())

    def names():
        return {tool["function"]["name"] for tool in agent.available_tools()}

    assert names() == {"list_documents"}
    agent.execute("list_documents", {})
    assert names() == {"read_document"}
    agent.execute("read_document", {"source_id": "narrative"})
    assert "finish" not in names()
    assert "record_facts" in names()
    for source in agent.sources:
        agent.execute("read_document", {"source_id": source})
    for field in ("platform", "object", "rights", "placement", "demands", "event_date"):
        agent.execute("ask_clarification", {"field": field, "question": "Уточните сведения"})
    assert names() == {"finish"}


def test_role_agent_reads_context_and_repairs_invalid_result():
    class ScriptedProvider:
        def __init__(self):
            self.count = 0

        async def choose(self, messages, tools):
            actions = [
                ("submit_result", {"query": "Ozon фото", "rationale": "Ранний вывод"}),
                ("inspect_context", {}),
                ("submit_result", {"query": ""}),
                ("submit_result", {"query": "Ozon фото", "rationale": "По объекту и площадке"}),
            ]
            if self.count in {1, 3}:
                assert "error" in json.loads(messages[-1]["content"])
            name, args = actions[self.count]
            self.count += 1
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": str(self.count),
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            }, {}

    result = asyncio.run(
        role_agent(
            ScriptedProvider(),
            "search",
            "План поиска",
            {"platform": "Ozon"},
            SearchPlan,
            lambda *args: None,
        )
    )
    assert result["query"] == "Ozon фото"


def test_pipeline_preserves_source_failure(tmp_path):
    class BrokenSource:
        async def search(self, query):
            raise AgentError("Источник недоступен")

    async def run():
        payload = sample()
        intake = await IntakeAgent(payload, Settings()).run(lambda *args: None)
        payload["mode"] = "yandex"  # baseline stages, real source requested
        result = await SearchPipeline(Settings(), source=BrokenSource()).run(
            payload, intake, lambda *args: None
        )
        assert result["status"] == "partial"
        assert result["coverage"]["remote_failed"] is True
        assert result["warnings"] == ["Источник недоступен"]

    asyncio.run(run())


def test_empty_demo_does_not_claim_external_search():
    class ForbiddenNetwork:
        async def search(self, query):
            raise AssertionError("Demo must not call the external source")

    async def run():
        payload = sample()
        intake = await IntakeAgent(payload, Settings()).run(lambda *args: None)
        result = await SearchPipeline(Settings(), source=ForbiddenNetwork()).run(
            payload, intake, lambda *args: None
        )
        assert result["status"] == "not_searched"
        assert result["external_search_attempted"] is False
        assert "не выполнялся" in result["report"]["summary"]

    asyncio.run(run())
