"""Comparison agent: inspect materials, read evidence, record grounded findings."""

from pydantic import Field

from app.agents.runtime import tool_loop
from app.contracts import ComparisonResult
from app.schemas import Empty, StrictModel


class ReadEvidence(StrictModel):
    evidence_ids: list[str] = Field(min_length=1, max_length=30)


class RecordComparisons(StrictModel):
    comparisons: list[ComparisonResult] = Field(min_length=1, max_length=10)


class RequestResearch(StrictModel):
    act_id: str
    page: int = Field(ge=1, le=150)
    question: str = Field(min_length=5, max_length=500)


class ComparisonAgent:
    def __init__(self, provider):
        self.provider = provider

    async def run(self, intake, cases, evidence, emit, feedback=None, request_research=None):
        by_id = {item["id"]: item for item in evidence if item.get("text_verified") is True}
        case_ids = {item["id"] for item in cases}
        read, results = set(), {}
        inspected = False
        research_calls = 0

        async def handle(name, args):
            nonlocal inspected, research_calls
            if name == "inspect_cases":
                inspected = True
                return {"client_fields": intake, "acts": cases, "review_issues": feedback or [],
                        "evidence_index": [{"id": e["id"], "act_id": e["act_id"],
                                            "page": e["page"]} for e in evidence]}
            if not inspected:
                raise ValueError("Сначала прочитайте карточки")
            if name == "request_research":
                if args.act_id not in case_ids or research_calls >= 2:
                    raise ValueError("Неверный акт или исчерпан лимит дополнительных исследований")
                if request_research is None:
                    return {"available": False, "message": "Дополнительный исследователь не "
                            "подключён. Отметьте недостаточность данных и unknown."}
                research_calls += 1
                additional = await request_research(args.act_id, args.page, args.question)
                for item in additional:
                    if item.get("text_verified") is not True or item["act_id"] not in case_ids:
                        raise ValueError("Дополнительный материал не прошёл проверку")
                    by_id[item["id"]] = item
                    read.add(item["id"])
                return {"additional_evidence": additional}
            if name == "read_evidence":
                if any(identity not in by_id for identity in args.evidence_ids):
                    raise ValueError("Неизвестное доказательство")
                read.update(args.evidence_ids)
                return {"evidence": [by_id[identity] for identity in args.evidence_ids]}
            if name == "record_comparisons":
                batch = {}
                for comparison in args.comparisons:
                    if comparison.case_id not in case_ids:
                        raise ValueError("Неизвестный акт")
                    for finding in comparison.findings:
                        if finding.relation != "unknown" and not finding.evidence_ids:
                            raise ValueError("Нужна опора на источник")
                        for identity in finding.evidence_ids:
                            if (identity not in read or
                                    by_id[identity]["act_id"] != comparison.case_id):
                                raise ValueError("Ссылка должна принадлежать прочитанному акту")
                    value = comparison.model_dump()
                    value["similarities"] = "\n".join(
                        f.text for f in comparison.findings if f.relation == "similarity"
                    ) or "Подтверждённые совпадения не сформированы"
                    value["differences"] = "\n".join(
                        f.text for f in comparison.findings if f.relation == "difference"
                    ) or "Существенные отличия требуют проверки"
                    if not any(f.evidence_ids for f in comparison.findings):
                        value["relevance"] = "unknown"
                        value["perspective"] = "unknown"
                    batch[comparison.case_id] = value
                results.update(batch)
                return {"recorded": len(batch)}
            if set(results) != case_ids:
                raise ValueError("Обработайте все представленные акты, включая неприменимые")
            return {"_finished": True, "comparisons": list(results.values())}

        return await tool_loop(self.provider,
            "Ты сопоставляешь судебные акты с карточкой клиента. Сначала inspect_cases, "
            "затем read_evidence, record_comparisons и finish_comparison. Если контекст "
            "неполный, запроси страницу через request_research (не более 2 раз). Для каждого "
            "существенного совпадения/отличия укажи dimension, relation, text и evidence_ids. "
            "Известное о клиенте бери только из client_fields. Довод стороны не является "
            "выводом суда. При недостатке информации ставь unknown. Не считай исход дела "
            "достаточным для аналогии. favorable/unfavorable оценивай относительно позиции "
            "клиента; при неясной стороне выбирай unknown. Не вычисляй проценты выигрыша.",
            {"inspect_cases": (Empty, "Прочитать факты клиента, акты и индекс доказательств"),
             "read_evidence": (ReadEvidence, "Прочитать точные цитаты по ID"),
             "request_research": (RequestResearch, "Запросить дополнительную страницу"),
             "record_comparisons": (RecordComparisons, "Сохранить сравнения с доказательствами"),
             "finish_comparison": (Empty, "Завершить после обработки всех актов")},
            handle, emit, "comparison")
