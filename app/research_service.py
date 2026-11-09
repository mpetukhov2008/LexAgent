"""Source history and document hydration, separate from model reasoning."""

import re
from datetime import datetime, timezone

from app.agent import AgentError
from app.sources import date_key


class ResearchService:
    def __init__(self, source, library, settings, emit):
        self.source, self.library, self.settings, self.emit = source, library, settings, emit

    async def prepare(self, candidates, coverage, online=True):
        acts = {a.get("file_url") or a["id"]: a for a in candidates}
        histories = {}
        if online and hasattr(self.source, "get_case_history"):
            for number in dict.fromkeys(a["number"] for a in candidates):
                if not re.fullmatch(r"[АA]\d{1,3}-\d{1,8}/\d{4}", number):
                    continue
                self.emit("source_history", {"stage": "research", "message": number})
                siblings, history = await self.source.get_case_history(number)
                histories[history["number"]] = history
                for act in siblings:
                    key = act["file_url"]
                    if key in acts:
                        acts[key].update(
                            {k: v for k, v in act.items() if k not in {"pages", "quotes", "id"}}
                        )
                    else:
                        acts[key] = act
        for act in acts.values():
            if re.fullmatch(r"[АA]\d{1,3}-\d{1,8}/\d{4}", act["number"]):
                act["number"] = act["number"].upper().replace("A", "А")
            if act["number"] in histories:
                act["history"] = {
                    k: histories[act["number"]][k]
                    for k in (
                        "history_complete",
                        "scope",
                        "checked_at",
                        "legal_finality_verified",
                        "truncated",
                    )
                }
        # First reserve one act from each available instance of each selected case.
        ordered = sorted(
            acts.values(),
            key=lambda a: (
                bool(a.get("pages")),
                bool(re.search("решени|постановлен|resheni|postanovlen", a.get("kind", ""), re.I)),
                date_key(a.get("date", "")),
            ),
            reverse=True,
        )
        preferred, remaining, seen = [], [], set()
        for act in ordered:
            group = (act["number"], act.get("instance", "unknown"))
            if group not in seen:
                preferred.append(act)
                seen.add(group)
            else:
                remaining.append(act)
        selected = (preferred + remaining)[: self.settings.research_acts]
        aliases = {}
        for act in selected:
            old = act["id"]
            await self.hydrate(act)
            aliases[old] = act["id"]
        for act in acts.values():
            act.setdefault("court", "Не указан")
            act.setdefault("appeal_status", "Не проверен")
            if act not in selected and not act.get("pages"):
                act["read_error"] = "Оригинал ещё не загружен: лимит документов этапа"
        result, unique = [], set()
        for act in acts.values():
            identity = act.get("document_sha256") or act["id"]
            if identity not in unique:
                unique.add(identity)
                result.append(act)
        for history in histories.values():
            history["act_ids"] = list(dict.fromkeys(aliases.get(i, i) for i in history["act_ids"]))
            for link in history["links"]:
                link["from_act_id"] = aliases.get(link["from_act_id"], link["from_act_id"])
                link["to_act_id"] = aliases.get(link["to_act_id"], link["to_act_id"])
        coverage["case_histories"] = histories
        coverage["history_complete"] = bool(histories) and all(
            h["history_complete"] for h in histories.values()
        )
        coverage["history_scope"] = "published_sources_snapshot_not_legal_finality"
        return result

    async def hydrate(self, act):
        if not act.get("pages") and self.library:
            prior = self.library.get(act["id"])
            if prior and prior.get("pages"):
                for key in (
                    "id",
                    "pages",
                    "document_sha256",
                    "original_retained",
                    "retrieved_at",
                    "text_origin",
                    "extraction_quality",
                    "page_quality",
                    "source_act_id",
                ):
                    if key in prior:
                        act[key] = prior[key]
        if act.get("pages") and hasattr(self.source, "download_document"):
            try:
                age = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(act.get("retrieved_at", "").replace("Z", "+00:00"))
                ).total_seconds()
            except (ValueError, TypeError):
                age = 86401
            if act.get("provenance") != "user_import_unverified" and (
                not act.get("original_retained") or age > 86400
            ):
                # A changed PDF gets a new content ID; never pair old text with new bytes.
                act["pages"] = []
        if act.get("pages"):
            if act.get("history") and self.library:
                self.library.save(act)
            return
        try:
            self.emit("source_read", {"stage": "research", "message": act["number"]})
            act["pages"] = await self.source.read_act(act)
            if self.library:
                self.library.save(act)
            act.pop("read_error", None)
        except AgentError as exc:
            act["read_error"] = str(exc)
            if act.get("document_sha256") and self.library:
                self.library.save(act)
