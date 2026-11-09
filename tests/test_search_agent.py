import asyncio

from app.agent import AgentError
from app.search_agent import SearchAgent, counterexample_query, fuse_candidates, suggested_queries


def act(identity, text="", number=None):
    return {"id": identity, "number": number or identity, "pages": [
        {"page": 1, "text": text}
    ] if text else [], "file_url": "https://kad.arbitr.ru/" + identity}


def test_fusion_does_not_discard_external_before_ranking_and_groups_cases():
    weak = [act(str(i), "поставка оборудования") for i in range(10)]
    relevant = act("best", "Ozon фотографии инфографика исключительные права")
    sibling = act("appeal", "Ozon фотографии", "best")
    selected, stats = fuse_candidates([
        ("local", "Ozon фотографии", weak),
        ("external", "Ozon фотографии", [relevant, sibling]),
    ], ["Ozon фотографии инфографика"], 3)
    assert selected[0]["id"] == "best"
    assert stats["candidate_acts"] == 12
    assert stats["candidate_cases"] == 11
    assert len(selected) == 3
    assert selected[0]["retrieval"]["origins"][0]["source"] == "external"


def test_query_expansion_is_bounded_and_case_lookup_is_not_expanded():
    intake = {"fields": {"platform": [{"value": "Ozon"}], "object": [
        {"value": "фотографии и инфографика"}
    ]}}
    queries = suggested_queries(intake)
    assert len(queries) >= 2
    assert any("фотографии" in q for q in queries)
    assert any("инфографика" in q for q in queries)
    assert suggested_queries(intake, "А70-1/2026") == ["А70-1/2026"]


def test_failed_source_stops_repeated_calls_and_demo_uses_no_network():
    class Source:
        calls = 0

        async def search(self, query):
            self.calls += 1
            raise AgentError("Источник недоступен")

    source = Source()
    agent = SearchAgent(source)
    _, coverage, warnings = asyncio.run(agent.run({}, "", True, lambda *args: None))
    assert source.calls == 1 and coverage["remote_failed"]
    assert warnings == ["Источник недоступен"]
    asyncio.run(agent.run({}, "", False, lambda *args: None))
    assert source.calls == 1


def test_counterexample_search_does_not_forward_client_identifiers_from_model_text():
    query = counterexample_query({"fields": {"platform": [{"value": "Ozon"}]}},
                                 "Иванов +79991234567 private@example.test ИНН 1234567890 отказ")
    assert "Иванов" not in query and "1234567890" not in query and "@" not in query
    assert query.endswith("отказ") and "Ozon" in query
