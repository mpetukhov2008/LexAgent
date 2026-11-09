"""Versioned integration contracts; legacy pipeline dictionaries stay compatible."""

import hashlib
from typing import Literal, Protocol

from pydantic import Field, model_validator

from app.schemas import StrictModel

CONTRACT_VERSION = "2.1"
SUPPORTED_CONTRACT_VERSIONS = {"2.0", CONTRACT_VERSION}


def case_key(number: str) -> str:
    normalized = number.upper().replace("A", "А").replace(" ", "")
    return "case_" + hashlib.sha256(normalized.encode()).hexdigest()[:16]


class PageText(StrictModel):
    page: int = Field(ge=1)
    text: str


class CaseRecord(StrictModel):
    id: str
    number: str
    act_ids: list[str] = Field(default_factory=list)
    history_complete: bool = False
    warnings: list[str] = Field(default_factory=list)
    history_scope: str = "unknown"
    history_checked_at: str | None = None
    legal_finality_verified: bool = False


class JudicialAct(StrictModel):
    id: str
    case_key: str
    court: str = ""
    date: str = ""
    date_kind: str = "unknown"
    instance: str = "unknown"
    kind: str = ""
    source_effect: str = ""
    url: str = ""
    provenance: str = "source_unverified"
    pages: list[PageText] = Field(default_factory=list)
    document_sha256: str | None = None
    original_retained: bool = False
    retrieved_at: str | None = None
    text_origin: str = "unknown"
    extraction_quality: dict = Field(default_factory=dict)
    page_quality: list[dict] = Field(default_factory=list)
    history: dict = Field(default_factory=dict)


class Evidence(StrictModel):
    id: str
    # Legacy case_id denotes an act. case_key identifies the grouped case.
    case_id: str
    act_id: str
    case_key: str
    page: int = Field(ge=1)
    quote: str = Field(min_length=10, max_length=2000)
    observation: str = Field(default="", max_length=1500)
    assertion_type: Literal["party_argument", "court_finding", "unknown"] = "unknown"
    text_verified: bool = False
    category: Literal["claims", "objections", "evidence", "facts", "reasoning", "outcome",
                      "unknown"] = "unknown"

    @model_validator(mode="after")
    def legacy_identity(self):
        if self.case_id != self.act_id:
            raise ValueError("Legacy case_id must equal act_id; use case_key for a case")
        return self


class Finding(StrictModel):
    dimension: Literal[
        "object", "rights", "placement", "demands", "event_date", "platform", "other"
    ]
    relation: Literal["similarity", "difference", "unknown"]
    text: str = Field(min_length=5, max_length=1500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)


class ComparisonResult(StrictModel):
    case_id: str
    relevance: Literal["high", "medium", "low", "unknown"] = "unknown"
    perspective: Literal["favorable", "unfavorable", "mixed", "unknown"] = "unknown"
    similarities: str = ""
    differences: str = ""
    findings: list[Finding] = Field(default_factory=list, max_length=20)
    warnings: list[str] = Field(default_factory=list)


class ReviewIssue(StrictModel):
    id: str
    target: Literal["search", "research", "comparison", "report"]
    message: str = Field(min_length=3, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list)
    blocking: bool = True


class ReviewResult(StrictModel):
    verdict: Literal["acceptable", "revise", "insufficient"]
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=30)
    concerns: list[str] = Field(default_factory=list, max_length=30)
    sufficient: bool = False


class ResearchRequest(StrictModel):
    contract_version: str = CONTRACT_VERSION
    intake: dict
    acts: list[dict]
    request: str = "Исследовать существенные обстоятельства и источники"
    histories: dict = Field(default_factory=dict)


class ResearchResponse(StrictModel):
    evidence: list[Evidence] = Field(default_factory=list, max_length=100)
    warnings: list[str] = Field(default_factory=list)
    history_complete: bool = False
    unassessed_categories: dict[str, list[str]] = Field(default_factory=dict)


class ReviewRequest(StrictModel):
    contract_version: str = CONTRACT_VERSION
    intake: dict
    comparisons: list[ComparisonResult]
    evidence: list[Evidence]
    coverage: dict
    acts: list[JudicialAct] = Field(default_factory=list)


class ResearchAgent(Protocol):
    async def research(self, request: ResearchRequest) -> ResearchResponse: ...


class CriticAgent(Protocol):
    async def review(self, request: ReviewRequest) -> ReviewResult: ...


def normalize_evidence(items, acts):
    """Revalidate text server-side; never trust a model's verified flag."""
    accepted, rejected = [], 0
    by_id = {a["id"]: a for a in acts}
    seen = set()
    for raw in items:
        value = raw.model_dump() if isinstance(raw, Evidence) else raw
        aid = value.get("act_id", value.get("case_id"))
        act = by_id.get(aid)
        page = next(
            (p for p in (act or {}).get("pages", []) if p["page"] == value.get("page")), None
        )
        quote = value.get("quote", "")
        if not page or len(quote.strip()) < 10 or quote not in page["text"]:
            rejected += 1
            continue
        identity = hashlib.sha256(f"{aid}:{page['page']}:{quote}".encode()).hexdigest()[:20]
        if identity in seen:
            continue
        seen.add(identity)
        accepted.append(Evidence(
            id="ev_" + identity, case_id=aid, act_id=aid, case_key=case_key(act["number"]),
            page=page["page"], quote=quote, observation=value.get("observation", ""),
            assertion_type=value.get("assertion_type", "unknown"), text_verified=True,
            category=value.get("category", "unknown"),
        ).model_dump())
    return accepted, rejected


def registry(acts):
    cases, documents = {}, []
    for act in acts:
        key = case_key(act["number"])
        cases.setdefault(key, CaseRecord(id=key, number=act["number"]))
        history = act.get("history", {})
        if history:
            cases[key].history_complete = history.get("history_complete") is True
            cases[key].history_scope = history.get("scope", "unknown")
            cases[key].history_checked_at = history.get("checked_at")
            cases[key].warnings = history.get("warnings", [])
        if act["id"] not in cases[key].act_ids:
            cases[key].act_ids.append(act["id"])
        documents.append(JudicialAct(
            id=act["id"], case_key=key, court=act.get("court", ""), date=act.get("date", ""),
            url=act.get("file_url", ""), provenance=act.get("provenance", "source_unverified"),
            pages=act.get("pages", []),
            instance=act.get("instance", "unknown"),
            **{k: act[k] for k in ("kind", "source_effect", "date_kind", "document_sha256",
                                   "original_retained", "retrieved_at",
                                   "text_origin", "extraction_quality", "page_quality", "history")
               if k in act and act[k] is not None},
        ).model_dump())
    return [c.model_dump() for c in cases.values()], documents
