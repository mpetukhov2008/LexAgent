import asyncio

from app.agent import IntakeAgent
from app.config import Settings
from app.contracts import (
    Evidence,
    ResearchResponse,
    ReviewIssue,
    ReviewResult,
    normalize_evidence,
)
from app.pipeline import SearchPipeline
from tests.test_agent import sample
from tests.test_roles_v2 import Script


def test_team_hooks_and_one_bounded_comparison_revision():
    acts = [{"id": "a", "number": "А70-1/2026", "court": "Учебный источник",
             "appeal_status": "Не проверен", "pages": [{"page": 1,
             "text": "Учебный источник описывает самостоятельное размещение фотографий на Ozon."}]}]
    evidence, _ = normalize_evidence([{
        "case_id": "a", "page": 1, "quote": "самостоятельное размещение фотографий на Ozon",
    }], acts)
    identity = evidence[0]["id"]

    class Source:
        async def search(self, query):
            return acts, {"source": "synthetic"}

    class Researcher:
        calls = 0

        async def research(self, request):
            self.calls += 1
            return ResearchResponse(evidence=[Evidence.model_validate(evidence[0])])

    class Critic:
        calls = 0

        async def review(self, request):
            self.calls += 1
            if self.calls == 1:
                return ReviewResult(verdict="revise", issues=[ReviewIssue(
                    id="review1", target="comparison", message="Уточните различие",
                    evidence_ids=[identity],
                )])
            return ReviewResult(verdict="acceptable")

    comparison = {"comparisons": [{"case_id": "a", "relevance": "medium", "findings": [{
        "dimension": "placement", "relation": "similarity", "text": "Совпадает размещение",
        "evidence_ids": [identity],
    }]}]}
    comparison_steps = [
        ("inspect_cases", {}), ("read_evidence", {"evidence_ids": [identity]}),
        ("record_comparisons", comparison), ("finish_comparison", {}),
    ]
    provider = Script([
        ("inspect_context", {}), ("submit_result", {"query": "Ozon фотографии",
                                                  "rationale": "Поиск по объекту"}),
        *comparison_steps, *comparison_steps,
        ("get_verified_materials", {}),
        ("record_conclusion", {"text": "Учебный вывод о способе размещения",
                               "evidence_ids": [identity]}), ("finish_report", {}),
    ])
    researcher, critic = Researcher(), Critic()

    async def run():
        payload = sample()
        intake = await IntakeAgent(payload, Settings()).run(lambda *args: None)
        payload["mode"] = "gigachat"
        return await SearchPipeline(Settings(), source=Source(), provider=provider,
                                    research_agent=researcher, critic_agent=critic).run(
            payload, intake, lambda *args: None
        )

    result = asyncio.run(run())
    assert researcher.calls == 1 and critic.calls == 2
    assert result["critique"]["verdict"] == "acceptable"
    assert len(result["report"]["conclusions"]) == 1
    assert result["status"] == "partial"
