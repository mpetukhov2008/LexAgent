"""Report agent restricted to explicitly referenced, verified materials."""

from pydantic import Field

from app.agents.runtime import tool_loop
from app.schemas import Empty, StrictModel


class Conclusion(StrictModel):
    text: str = Field(min_length=10, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=15)
    category: str = Field(default="observation", max_length=50)


class FinishReport(StrictModel):
    next_steps: list[str] = Field(default_factory=list, max_length=10)
    missing_evidence: list[str] = Field(default_factory=list, max_length=10)


class ReportAgent:
    def __init__(self, provider):
        self.provider = provider

    async def run(self, intake, comparisons, evidence, critique, emit):
        issues = [issue for issue in critique.get("issues", []) if issue.get("blocking")]
        blocked = {identity for issue in issues for identity in issue.get("evidence_ids", [])}
        global_block = any(not issue.get("evidence_ids") for issue in issues)
        verified = {item["id"]: item for item in evidence
                    if item["text_verified"] and item["id"] not in blocked and not global_block}
        inspected, conclusions = False, []

        def handle(name, args):
            nonlocal inspected
            if name == "get_verified_materials":
                inspected = True
                return {"client_fields": intake, "comparisons": comparisons,
                        "evidence": list(verified.values()), "critique": critique}
            if not inspected:
                raise ValueError("Сначала прочитайте материалы")
            if name == "record_conclusion":
                if any(identity not in verified for identity in args.evidence_ids):
                    raise ValueError("Несуществующая ссылка")
                if len(conclusions) >= 12:
                    raise ValueError("Лимит выводов")
                value = args.model_dump()
                value["id"] = f"conclusion_{len(conclusions) + 1}"
                value["verification"] = "references_checked_semantics_require_review"
                conclusions.append(value)
                return {"recorded": value["id"]}
            summary = (
                f"Подготовлено {len(conclusions)} предварительных выводов "
                f"с опорой на {len(verified)} проверенных текстовых фрагментов. "
                "Смысловую обоснованность и полноту исследования должен проверить юрист."
                if conclusions else
                "Подтверждённых выводов для записки пока недостаточно. Требуется исследование."
            )
            return {"_finished": True, "summary": summary, "conclusions": conclusions,
                    "next_steps": args.next_steps, "missing_evidence": args.missing_evidence,
                    "review_status": critique.get("verdict", "insufficient")}

        return await tool_loop(self.provider,
            "Ты составляешь исследовательскую записку для юриста. Сначала прочитай "
            "get_verified_materials. Каждый существенный судебный вывод сохраняй отдельно "
            "через record_conclusion с существующими evidence_ids. Учитывай замечания "
            "критика, не повторяй заблокированные тезисы. Не добавляй новые дела, нормы, "
            "номера страниц, цитаты и проценты выигрыша. Не называй вывод окончательным. "
            "Заверши finish_report: next_steps — действия для юриста, missing_evidence — "
            "недостающие материалы. Эти списки не должны содержать новые утверждения о суде.",
            {"get_verified_materials": (Empty, "Прочитать доказательства, сравнения и замечания"),
             "record_conclusion": (Conclusion, "Сохранить тезис со ссылками на доказательства"),
             "finish_report": (FinishReport, "Завершить и указать следующие действия")},
            handle, emit, "report")
