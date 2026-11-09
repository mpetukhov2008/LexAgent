import asyncio
import json

from app.agents.comparison import ComparisonAgent
from app.agents.report import ReportAgent
from app.budget import BudgetedProvider
from app.contracts import normalize_evidence


class Script:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.errors = []

    async def choose(self, messages, tools=None):
        if messages and messages[-1]["role"] == "tool":
            self.errors.append("error" in json.loads(messages[-1]["content"]))
        name, args = next(self.steps)
        return {"role": "assistant", "content": "", "tool_calls": [{
            "id": name + str(len(self.errors)),
            "function": {"name": name, "arguments": json.dumps(args)},
        }]}, {}


def material():
    cases = [{"id": "a", "number": "А70-1/2026", "pages": [{
        "page": 1, "text": "Суд установил самостоятельное размещение фотографий на Ozon."
    }]}]
    evidence, _ = normalize_evidence([{
        "case_id": "a", "page": 1, "quote": "самостоятельное размещение фотографий на Ozon",
    }], cases)
    return cases, evidence


def test_comparison_rejects_unread_and_cross_act_references_then_repairs():
    cases, evidence = material()
    identity = evidence[0]["id"]
    finding = {"dimension": "placement", "relation": "similarity", "text": "Совпадает размещение",
               "evidence_ids": [identity]}
    comparisons = {"comparisons": [{"case_id": "a", "relevance": "medium", "findings": [finding]}]}
    provider = Script([
        ("inspect_cases", {}), ("record_comparisons", comparisons),
        ("read_evidence", {"evidence_ids": [identity]}),
        ("record_comparisons", comparisons), ("finish_comparison", {}),
    ])
    result = asyncio.run(ComparisonAgent(provider).run({}, cases, evidence, lambda *args: None))
    assert result["comparisons"][0]["findings"][0]["evidence_ids"] == [identity]
    assert True in provider.errors


def test_report_rejects_invented_references_and_blocked_evidence():
    _, evidence = material()
    identity = evidence[0]["id"]
    provider = Script([
        ("get_verified_materials", {}),
        ("record_conclusion", {"text": "Источник описывает размещение", "evidence_ids": ["fake"]}),
        ("record_conclusion", {"text": "Источник описывает размещение", "evidence_ids": [identity]}),
        ("finish_report", {"next_steps": ["Проверить историю"]}),
    ])
    result = asyncio.run(ReportAgent(provider).run({}, [], evidence, {}, lambda *args: None))
    assert len(result["conclusions"]) == 1 and True in provider.errors
    blocked = Script([
        ("get_verified_materials", {}),
        ("record_conclusion", {"text": "Источник описывает размещение", "evidence_ids": [identity]}),
        ("finish_report", {}),
    ])
    result = asyncio.run(ReportAgent(blocked).run({}, [], evidence, {"issues": [{
        "blocking": True, "evidence_ids": [identity]
    }]}, lambda *args: None))
    assert result["conclusions"] == []


def test_shared_budget_reserves_calls_before_provider():
    from app.agent import AgentError

    state, saved = {}, []
    provider = BudgetedProvider(Script([("finish", {})]), state, 1, lambda: saved.append(True))

    async def run():
        await provider.choose([])
        try:
            await provider.choose([])
        except AgentError:
            return
        raise AssertionError("Budget was bypassed")

    asyncio.run(run())
    assert state["model_calls"] == 1 and saved == [True]
