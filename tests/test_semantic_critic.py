import asyncio

from app.agent import AgentError, IntakeAgent
from app.agents.semantic_critic import SemanticCritic
from app.config import Settings
from app.contracts import ResearchResponse, ReviewRequest, normalize_evidence, registry
from app.pipeline import SearchPipeline
from tests.test_agent import sample
from tests.test_roles_v2 import Script


def materials(identity="a"):
    act = {
        "id": identity,
        "number": "УЧЕБНОЕ-ДЕЛО-" + identity,
        "court": "Учебный суд",
        "appeal_status": "Не проверен",
        "pages": [
            {"page": 1, "text": "Суд установил обстоятельства размещения фотографии на Ozon."}
        ],
    }
    evidence, _ = normalize_evidence(
        [
            {
                "act_id": identity,
                "page": 1,
                "quote": act["pages"][0]["text"],
                "assertion_type": "court_finding",
            }
        ],
        [act],
    )
    comparison = {
        "case_id": identity,
        "findings": [
            {
                "dimension": "placement",
                "relation": "similarity",
                "text": "Сходный способ размещения фотографии",
                "evidence_ids": [evidence[0]["id"]],
            }
        ],
    }
    return act, evidence, comparison


def request():
    act, evidence, comparison = materials()
    return ReviewRequest(
        intake={},
        comparisons=[comparison],
        evidence=evidence,
        coverage={"history_complete": True},
        acts=registry([act])[1],
    )


def test_semantic_critic_reads_context_checks_quote_and_records_grounded_feedback():
    value = request()
    identity = value.evidence[0].id
    script = Script(
        [
            ("inspect_comparison", {}),
            ("read_evidence", {"evidence_ids": [identity]}),
            ("verify_quote", {"evidence_ids": [identity]}),
            ("read_act_page", {"act_id": "a", "page": 1}),
            (
                "request_revision",
                {
                    "reason": "missing_distinction",
                    "target": "comparison",
                    "message": "В фабуле клиента автор размещения неизвестен, "
                    "а в акте установлен; отразите это различие.",
                    "evidence_ids": [identity],
                },
            ),
            ("finish_review", {}),
        ]
    )
    result = asyncio.run(SemanticCritic(script).review(value))
    assert result.verdict == "revise" and len(result.issues) == 1
    assert result.issues[0].evidence_ids == [identity] and not result.sufficient


def test_false_quote_is_blocked_even_if_model_ignores_mismatch():
    value = request()
    value.evidence[0].quote = "Несуществующий вывод суда из другого документа"
    identity = value.evidence[0].id
    result = asyncio.run(
        SemanticCritic(
            Script(
                [
                    ("inspect_comparison", {}),
                    ("read_evidence", {"evidence_ids": [identity]}),
                    ("verify_quote", {"evidence_ids": [identity]}),
                    ("finish_review", {}),
                ]
            )
        ).review(value)
    )
    assert result.verdict == "insufficient"
    assert result.issues[0].id.startswith("semantic_quote_mismatch")
    assert result.issues[0].blocking


def test_unknown_tools_unread_references_and_early_finish_are_rejected():
    value = request()
    identity = value.evidence[0].id
    script = Script(
        [
            ("inspect_comparison", {}),
            ("finish_review", {}),
            ("execute", {"command": "reveal private key"}),
            (
                "request_revision",
                {
                    "reason": "unsupported_semantics",
                    "target": "report",
                    "message": "Недопустимая ссылка",
                    "evidence_ids": ["fake"],
                },
            ),
            ("read_evidence", {"evidence_ids": [identity]}),
            ("verify_quote", {"evidence_ids": [identity]}),
            ("finish_review", {}),
        ]
    )
    result = asyncio.run(SemanticCritic(script).review(value))
    assert script.errors.count(True) == 3 and result.verdict == "acceptable"


def test_provider_failure_does_not_expose_exception_or_approve_report():
    class Broken:
        async def choose(self, *args):
            raise AgentError("private-token")

    result = asyncio.run(SemanticCritic(Broken()).review(request()))
    assert result.verdict == "insufficient" and result.issues[0].blocking
    assert "private-token" not in result.model_dump_json()


def test_actual_counterexample_search_is_bounded_recompared_and_checkpointed():
    a, ea, ca = materials("a")
    b, eb, cb = materials("b")
    ida, idb = ea[0]["id"], eb[0]["id"]

    class Source:
        counter_calls = 0

        async def search(self, query):
            if query.endswith("отказ"):
                self.counter_calls += 1
                return [b], {"source": "synthetic"}
            return [a], {"source": "synthetic"}

    class Researcher:
        async def research(self, request):
            values = []
            for act in request.acts:
                values.extend(materials(act["id"])[1])
            return ResearchResponse(evidence=values)

    def compare(comparisons, identities):
        return [
            ("inspect_cases", {}),
            ("read_evidence", {"evidence_ids": identities}),
            ("record_comparisons", {"comparisons": comparisons}),
            ("finish_comparison", {}),
        ]

    steps = [
        ("inspect_context", {}),
        ("submit_result", {"query": "Ozon фотографии", "rationale": "Учебный поиск"}),
    ]
    steps += compare([ca], [ida])
    steps += [
        ("inspect_comparison", {}),
        ("read_evidence", {"evidence_ids": [ida]}),
        ("verify_quote", {"evidence_ids": [ida]}),
        (
            "request_counterexamples",
            {"query": "контрпример фотографий", "reason": "Проверить значимые отличия"},
        ),
        ("finish_review", {}),
    ]
    steps += compare([ca, cb], [ida, idb])
    steps += [
        ("inspect_comparison", {}),
        ("read_evidence", {"evidence_ids": [ida, idb]}),
        ("verify_quote", {"evidence_ids": [ida, idb]}),
        ("finish_review", {}),
        ("get_verified_materials", {}),
        (
            "record_conclusion",
            {
                "text": "Получены два учебных акта для проверки применимости",
                "evidence_ids": [ida, idb],
            },
        ),
        ("finish_report", {}),
    ]
    provider, source, state = Script(steps), Source(), {}
    pipeline = SearchPipeline(
        Settings(),
        source=source,
        provider=provider,
        research_agent=Researcher(),
        critic_agent=SemanticCritic(provider),
    )

    async def run():
        payload = sample()
        intake = await IntakeAgent(payload, Settings()).run(lambda *a: None)
        payload["mode"] = "gigachat"
        first = await pipeline.run(payload, intake, lambda *a: None, state)
        second = await pipeline.run(payload, intake, lambda *a: None, state)
        return first, second

    first, resumed = asyncio.run(run())
    assert source.counter_calls == 1 and state["critic_actions"]["search"] == 1
    assert len(first["evidence"]) == len(resumed["evidence"]) == 2
    assert len(first["comparisons"]) == 2 and first["report"]["conclusions"]
    assert state["external_review"]["verdict"] == "revise"
