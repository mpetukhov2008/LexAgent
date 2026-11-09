"""Bounded multi-query retrieval and fusion before selecting research material."""

import re

from app.agent import AgentError
from app.contracts import case_key

STOP_WORDS = {"и", "или", "на", "в", "по", "из", "для", "это", "товара", "суд"}


def terms(text):
    return list(dict.fromkeys(
        word[:7] for word in re.findall(r"[а-яёa-z0-9]{3,}", text.casefold())
        if word not in STOP_WORDS
    ))[:24]


def suggested_queries(intake, preferred=""):
    if re.fullmatch(r"[АаAa]\d{1,3}-\d{1,8}/\d{4}", preferred.strip()):
        return [preferred.strip()]
    fields = intake.get("fields", {})
    values = {key: " ".join(f["value"] for f in fields.get(key, [])) for key in fields}
    platform_text = values.get("platform", "").casefold()
    platform = "Wildberries" if "wildberries" in platform_text else (
        "Ozon" if "ozon" in platform_text else "маркетплейс"
    )
    obj = values.get("object", "").casefold()
    objects = [word for stem, word in [
        ("фото", "фотографии"), ("инфограф", "инфографика"),
        ("текст", "текст карточки"), ("оформ", "оформление карточки"),
    ] if stem in obj] or ["контент карточки"]
    queries = [preferred.strip()] if preferred.strip() else []
    queries.extend(f"{platform} {word}" for word in objects)
    placement = values.get("placement", "").casefold()
    if "общ" in placement or "присоедин" in placement:
        queries.append(f"{platform} общая карточка фотографии")
    elif "автомат" in placement:
        queries.append(f"{platform} автоматическое размещение контента")
    queries.append(f"{platform} исключительные права {objects[0]}")
    return list(dict.fromkeys(q[:250] for q in queries if len(q) >= 3))[:4]


def fuse_candidates(batches, queries, limit=6):
    """Reciprocal-rank fusion plus explainable textual matches, never win probability."""
    merged = {}
    tokens = terms(" ".join(queries))
    for source, query, rows in batches:
        for rank, act in enumerate(rows, 1):
            identity = act.get("file_url") or act["id"]
            if identity not in merged:
                merged[identity] = {
                    **act, "retrieval": {"score": 0.0, "matches": [], "origins": []}
                }
            item = merged[identity]
            if not item.get("pages") and act.get("pages"):
                item["pages"] = act["pages"]
            origin = {"source": source, "query": query, "rank": rank}
            if origin not in item["retrieval"]["origins"]:
                item["retrieval"]["origins"].append(origin)
                item["retrieval"]["score"] += 1 / (60 + rank)
    for item in merged.values():
        text = " ".join([
            item.get("number", ""), item.get("kind", ""),
            *(page["text"] for page in item.get("pages", [])),
        ]).casefold()
        matched = [token for token in tokens if token in text]
        item["retrieval"]["matches"] = matched
        item["retrieval"]["score"] += min(len(matched), 12) * 0.003
        item["retrieval"]["score"] = round(item["retrieval"]["score"], 6)
    ranked = sorted(merged.values(), key=lambda x: (-x["retrieval"]["score"], x["id"]))
    # Diversity first, retaining sibling acts for an eventual case researcher.
    selected, siblings, seen = [], [], set()
    for item in ranked:
        key = case_key(item["number"])
        item["case_key"] = key
        if key in seen:
            siblings.append(item)
        else:
            seen.add(key)
            selected.append(item)
    ordered = selected + siblings
    return ordered[:limit], {
        "candidate_acts": len(ranked), "candidate_cases": len(seen),
        "selected_acts": min(len(ordered), limit),
        "excluded": [{"id": a["id"], "number": a["number"],
                      "reason": "Лимит материалов после объединения и ранжирования"}
                     for a in ordered[limit:]],
    }


class SearchAgent:
    def __init__(self, source, library=None, max_queries=3, max_acts=6):
        self.source, self.library = source, library
        self.max_queries, self.max_acts = max_queries, max_acts

    async def run(self, intake, preferred, online, emit, extra_queries=None):
        queries = list(dict.fromkeys(
            suggested_queries(intake, preferred) + (extra_queries or [])
        ))[:self.max_queries]
        batches, journal, warnings = [], [], []
        remote_failed = False
        remote_ok = 0
        for query in queries:
            entry = {"query": query, "local_count": 0, "remote_count": 0,
                     "external_attempted": online}
            emit("search_query", {"stage": "search", "message": query})
            if self.library:
                rows = self.library.search(query)
                batches.append(("local", query, rows))
                entry["local_count"] = len(rows)
            if online:
                try:
                    rows, coverage = await self.source.search(query)
                    batches.append(("external", query, rows))
                    entry.update(remote_count=len(rows), coverage=coverage)
                    remote_ok += 1
                except AgentError as exc:
                    remote_failed = True
                    entry["error"] = str(exc)
                    if str(exc) not in warnings:
                        warnings.append(str(exc))
            journal.append(entry)
            # Do not repeatedly spend calls on rejected credentials or an unavailable source.
            if remote_failed and not remote_ok:
                break
        cases, stats = fuse_candidates(batches, queries, self.max_acts)
        return cases, {
            "source": "local+ras" if online else "local", "queries": journal,
            "external_search_attempted": online, "remote_failed": remote_failed,
            "completed_queries": len(journal), "planned_queries": len(queries),
            "ranking": "reciprocal_rank_fusion+lexical", **stats,
        }, warnings
