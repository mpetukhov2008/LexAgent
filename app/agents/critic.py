"""Opt-in structural critic. No model calls or claims of semantic verification."""

import hashlib
import re

from app.agent import AgentError
from app.contracts import CONTRACT_VERSION, ReviewIssue, ReviewRequest, ReviewResult

ISSUE_REASONS = frozenset({
    "history_unknown", "source_failed", "no_evidence", "duplicate_evidence",
    "unverified_evidence", "missing_reference", "cross_act_reference",
    "unsupported_finding", "unsupported_comparison", "party_as_court", "review_truncated",
})
# This narrow textual rule flags an explicit attribution; it is not a legal classifier.
COURT_ATTRIBUTION = re.compile(
    r"^\s*суд(?:\s+апелляционной\s+инстанции|\s+кассационной\s+инстанции)?"
    r"\s+(?:установил|признал|приш[её]л\s+к\s+выводу|сч[её]л)\b", re.IGNORECASE,
)


def issue_reason(issue: ReviewIssue) -> str:
    """Machine-readable reason without changing the shared contract 2.0."""
    return issue.id.split(":", 1)[0]


class CriticAgent:
    def __init__(self, settings=None):
        # The trusted team-module loader passes Settings, never an LLM provider.
        pass

    async def review(self, request: ReviewRequest) -> ReviewResult:
        request = ReviewRequest.model_validate(request)
        if request.contract_version != CONTRACT_VERSION:
            raise AgentError("Критик: неподдерживаемая версия контракта")
        if len(request.comparisons) > 10 or len(request.evidence) > 100:
            raise AgentError("Критик: превышен лимит материалов")
        issues, total = [], 0

        def add(reason, target, message, refs=(), *, blocking=True, scope=""):
            nonlocal total
            total += 1
            # Reserve one slot for a blocking overflow notice, never silently approve.
            if len(issues) < 29:
                identity = hashlib.sha256(
                    f"{reason}:{scope}:{list(refs)}".encode()
                ).hexdigest()[:16]
                issues.append(ReviewIssue(id=f"{reason}:{identity}", target=target,
                                          message=message, evidence_ids=list(refs),
                                          blocking=blocking))

        if request.coverage.get("history_complete") is not True:
            add("history_unknown", "research", "Полнота истории обжалования не подтверждена; "
                "окончательность акта неизвестна.", blocking=False)
        if request.coverage.get("remote_failed") is True:
            add("source_failed", "search", "Есть сбой судебного источника; полнота поиска "
                "не подтверждена.", blocking=False)
        if not request.evidence:
            add("no_evidence", "research", "Для сравнения не переданы доказательства.")

        by_id, duplicates = {}, set()
        for evidence in request.evidence:
            if evidence.id in by_id:
                duplicates.add(evidence.id)
            by_id[evidence.id] = evidence
        for identity in sorted(duplicates):
            add("duplicate_evidence", "research", "Идентификатор доказательства неоднозначен.",
                [identity], scope=identity)
        for evidence in by_id.values():
            if not evidence.text_verified:
                add("unverified_evidence", "research", "Дословность фрагмента не проверена "
                    "по тексту акта.", [evidence.id], scope=evidence.id)

        for ci, comparison in enumerate(request.comparisons):
            if not comparison.findings and (comparison.relevance != "unknown" or
                                            comparison.perspective != "unknown"):
                add("unsupported_comparison", "comparison", "Оценка применимости дана "
                    "без структурированных оснований.", scope=str(ci))
            for fi, finding in enumerate(comparison.findings):
                scope = f"{ci}:{fi}"
                if finding.relation != "unknown" and not finding.evidence_ids:
                    add("unsupported_finding", "comparison", "У существенного сопоставления "
                        "нет ссылки на доказательство.", scope=scope)
                supporting = []
                for identity in dict.fromkeys(finding.evidence_ids):
                    evidence = by_id.get(identity)
                    if evidence is None:
                        # Unknown IDs must NOT enter evidence_ids: pipeline validates that field.
                        add("missing_reference", "comparison", "Сопоставление содержит ссылку "
                            "на отсутствующее доказательство; исправьте ссылку.",
                            scope=f"{scope}:{identity}")
                    elif evidence.act_id != comparison.case_id:
                        add("cross_act_reference", "comparison", "Ссылка сопоставления "
                            "относится к другому акту.", [identity], scope=scope)
                    elif identity not in duplicates:
                        supporting.append(evidence)
                if (COURT_ATTRIBUTION.search(finding.text) and supporting and
                        all(e.assertion_type == "party_argument" for e in supporting)):
                    add("party_as_court", "comparison", "Тезис явно приписан суду, но все "
                        "его доступные опоры помечены как доводы стороны; проверьте атрибуцию.",
                        [e.id for e in supporting], scope=scope)

        if total > 29:
            issues.append(ReviewIssue(id="review_truncated:limit", target="report",
                                      message="Превышен лимит замечаний; необходима полная "
                                              "ручная проверка, отчёт ограничен.", blocking=True))
        if any(i.target == "comparison" and i.blocking for i in issues):
            verdict = "revise"
        elif issues:
            verdict = "insufficient"
        else:
            verdict = "acceptable"
        return ReviewResult(verdict=verdict, issues=issues, sufficient=False, concerns=[
            "Выполнены только структурные проверки и узкое правило явной атрибуции. "
            "Смысл, юридическая применимость и оригиналы цитат требуют проверки юристом.",
        ])
