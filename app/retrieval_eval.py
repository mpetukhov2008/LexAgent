"""Case-level retrieval evaluation with explicit provisional/expert annotation states."""

import hashlib
import json
import math
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.contracts import case_key
from app.schemas import StrictModel
from app.search_agent import fuse_candidates, terms


class Annotation(StrictModel):
    status: Literal["pending", "confirmed"] = "pending"
    assigned_to: str = "participant_5"
    reviewer: str | None = None
    reviewed_at: datetime | None = None
    note: str = ""

    @model_validator(mode="after")
    def attestation(self):
        if self.status == "confirmed" and (not self.reviewer or not self.reviewed_at):
            raise ValueError("Confirmed labels require a named reviewer and review time")
        return self


class RetrievalTask(StrictModel):
    id: str
    split: Literal["dev", "test"]
    query: str = Field(min_length=3, max_length=1000)
    scenario: str
    source_case_groups: list[str] = Field(min_length=1)
    source_refs: list[dict]
    labels: dict[str, Literal[0, 1, 2]]
    annotation: Annotation

    @field_validator("labels", mode="before")
    @classmethod
    def explicit_grades(cls, value):
        if not isinstance(value, dict) or any(
            type(v) is not int or v not in (0, 1, 2) for v in value.values()
        ):
            raise ValueError("Use explicit integer relevance judgments")
        return value


def load_tasks(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(5_000_001)
    if len(raw) > 5_000_000:
        raise ValueError("Corpus too large")
    tasks, seen, groups = [], set(), {}
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        task = RetrievalTask.model_validate_json(line)
        if task.id in seen:
            raise ValueError("Duplicate query identity")
        seen.add(task.id)
        for group in task.source_case_groups:
            if group in groups and groups[group] != task.split:
                raise ValueError("Source case leakage across dev/test")
            groups[group] = task.split
        tasks.append(task)
    if not tasks:
        raise ValueError("Empty corpus")
    return tasks, hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def historical_acts(root):
    """Known report snapshot; provenance remains unverified until original review."""
    path = Path(root) / "reports/ras-gigachat-verified-live.json"
    found = {}

    def walk(value):
        if isinstance(value, dict):
            if value.get("id") and value.get("pages") and value.get("file_url"):
                found[value["id"]] = value
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(json.loads(path.read_text(encoding="utf-8")))
    return list(found.values())


def lexical_rank(acts, query):
    tokens = terms(query)
    scored = [
        (sum(t in " ".join(p["text"] for p in a["pages"]).casefold() for t in tokens), a)
        for a in acts
    ]
    return [a for score, a in sorted(scored, key=lambda pair: (-pair[0], pair[1]["id"])) if score]


def unique_cases(acts):
    return list(dict.fromkeys(case_key(a["number"]) for a in acts))


def rank_metrics(ranking, labels):
    ranking = list(dict.fromkeys(ranking))
    relevant = {identity for identity, grade in labels.items() if grade > 0}
    return {
        "precision_at_5": sum(i in relevant for i in ranking[:5]) / 5,
        "recall_at_20": (
            sum(i in relevant for i in ranking[:20]) / len(relevant) if relevant else None
        ),
    }


def evaluate(tasks, acts, *, split="test", allow_draft=False):
    selected = [t for t in tasks if t.split == split]
    if not selected:
        raise ValueError("Empty evaluation split")
    if not allow_draft and any(t.annotation.status != "confirmed" for t in selected):
        raise ValueError("Expert review is pending; use --allow-draft only for provisional metrics")
    pool = {case_key(a["number"]) for a in acts}
    if any(set(t.labels) != pool for t in selected):
        raise ValueError("Every pooled case must have a judgment for each evaluated query")
    report = {
        "split": split,
        "tasks": len(selected),
        "pool_cases": len(pool),
        "pool_acts": len(acts),
        "expert_confirmed": all(t.annotation.status == "confirmed" for t in selected),
        "variants": {},
        "limitations": [
            "Оценка относится только к известному пулу, не ко всей судебной практике.",
            "Пул исторического отчёта мал и требует сверки с оригиналами.",
            "Предварительные метки не являются экспертной оценкой применимости.",
            "Сравниваются лексический поиск и объединение запросов; качество "
            "смыслового критика и юридического отчёта этими числами не измеряется.",
        ],
    }
    for variant in ("lexical", "multi_query_fusion"):
        results, durations = [], []
        for task in selected:
            started = time.perf_counter()
            ranked = lexical_rank(acts, task.query)
            if variant == "multi_query_fusion":
                queries = [task.query, "маркетплейс фотографии доказательства исключительные права"]
                ranked, _ = fuse_candidates(
                    [("snapshot", q, lexical_rank(acts, q)) for q in queries], queries, len(acts)
                )
            ranking = unique_cases(ranked)
            durations.append((time.perf_counter() - started) * 1000)
            results.append(
                {"id": task.id, "ranking": ranking, **rank_metrics(ranking, task.labels)}
            )
        recalls = [r["recall_at_20"] for r in results if r["recall_at_20"] is not None]
        report["variants"][variant] = {
            "precision_at_5": statistics.mean(r["precision_at_5"] for r in results),
            "recall_at_20": statistics.mean(recalls) if recalls else None,
            "latency_ms_p50": statistics.median(durations),
            "latency_ms_p95": sorted(durations)[math.ceil(len(durations) * 0.95) - 1],
            "results": results,
        }
    return report
