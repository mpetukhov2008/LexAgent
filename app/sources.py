"""Bounded, cached Parser API access. No raw upstream errors cross the boundary."""

import asyncio
import base64
import hashlib
import io
import json
import logging
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pypdf import PdfReader

from app.agent import AgentError
from app.config import ROOT
from app.documents import OriginalStore
from app.pdf_processing import extract_document


class RedactQueryCredentials(logging.Filter):
    def filter(self, record):
        record.msg = re.sub(r"([?&]key=)[^&\s\"']+", r"\1[REDACTED]", record.getMessage())
        record.args = ()
        return True


for _logger_name in ("httpx", "httpcore"):
    logging.getLogger(_logger_name).addFilter(RedactQueryCredentials())


class TransientSourceError(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


def instance_name(value):
    text = str(value).casefold()
    for stem, name in (("перв", "first"), ("апел", "appeal"),
                       ("кассац", "cassation"), ("верхов", "supreme")):
        if stem in text:
            return name
    return "unknown"


def date_key(value):
    for pattern in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(value)[:10], pattern).isoformat()
        except ValueError:
            pass
    return ""


def source_act(number, url, *, court="", date="", kind="", instance="unknown",
               case_url="", source="ras", effect=""):
    return {"id": hashlib.sha256(url.encode()).hexdigest()[:16], "number": str(number)[:100],
            "court": str(court)[:200], "date": str(date)[:40], "kind": str(kind)[:300],
            "date_kind": "source_registration_or_publication",
            "instance": instance,
            "file_url": url, "case_url": case_url, "source": source,
            "provenance": "parser_api_" + source + "_unverified",
            "appeal_status": "Окончательность акта не подтверждена",
            "source_effect": str(effect)[:1200], "pages": [], "quotes": []}


def court_url(value):
    try:
        if (not isinstance(value, str) or not value or len(value) > 2000 or
                any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value) or
                "\\" in value):
            return False
        url = urlsplit(value)
        return (
            url.scheme == "https"
            and url.hostname in {"kad.arbitr.ru", "ras.arbitr.ru"}
            and url.port in {None, 443}
            and url.username is None
            and url.password is None
            and not url.fragment
        )
    except (ValueError, TypeError):
        return False


class ParserClient:
    def __init__(self, settings, transport=None, cache_dir=None, extractor=None):
        self.settings = settings
        self.transport = transport
        self.cache_dir = Path(cache_dir or ROOT / "data/source-cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.originals = OriginalStore(Path(cache_dir) / "originals" if cache_dir
                                       else settings.document_dir)
        self.extractor = extractor
        self._budget, self._save_budget = {}, lambda: None

    @property
    def calls(self):
        return self._budget.get("source_calls", 0)

    def set_budget(self, budget, save):
        self._budget, self._save_budget = budget, save

    async def request(self, endpoint, params, *, fresh=False):
        if endpoint not in {"arbitr_api/details_by_number", "ras_arbitr_api/search",
                            "arbitr_api/pdf_download", "ras_arbitr_api/pdf_download"}:
            raise AgentError("Неизвестная операция судебного источника")
        if "key" in params:
            raise AgentError("Недопустимые параметры источника")
        cache_key = hashlib.sha256(
            json.dumps([hashlib.sha256(self.settings.parser_api_key.encode()).hexdigest(),
                        endpoint, params], sort_keys=True).encode()
        ).hexdigest()
        target = self.cache_dir / (cache_key + ".json")
        if not fresh and target.exists() and time.time() - target.stat().st_mtime < 86400:
            try:
                cached = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and cached.get("done", cached.get("Success")) == 1:
                    return cached
            except (ValueError, OSError):
                pass
        if not self.settings.parser_api_key:
            raise AgentError("Parser API не настроен")
        for attempt in range(self.settings.source_attempts):
            if self.calls >= self.settings.source_max_calls:
                raise AgentError(f"Достигнут лимит {self.settings.source_max_calls} "
                                 "обращений к источнику за запуск")
            self._budget["source_calls"] = self.calls + 1
            self._save_budget()
            retry_after = 0
            try:
                return await self._request_once(endpoint, params, target)
            except TransientSourceError as exc:
                retry_after = exc.retry_after
                if attempt + 1 >= self.settings.source_attempts:
                    raise AgentError("Судебный источник временно недоступен; повторы исчерпаны")
            await asyncio.sleep(min(2, max(retry_after,
                                          self.settings.source_backoff * (2 ** attempt))))
        raise AgentError("Судебный источник недоступен")

    async def _request_once(self, endpoint, params, target):
        try:
            async with httpx.AsyncClient(
                timeout=75, transport=self.transport, follow_redirects=False
            ) as client:
                async with client.stream(
                    "GET",
                    "https://parser-api.com/parser/" + endpoint,
                    params={"key": self.settings.parser_api_key, **params},
                ) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 16_000_000:
                            raise AgentError("Ответ источника превышает 16 МБ")
                    if response.status_code in {429, 500, 502, 503, 504}:
                        try:
                            retry_after = float(response.headers.get("Retry-After", 0))
                        except ValueError:
                            retry_after = 0
                        raise TransientSourceError(retry_after)
                    if response.status_code != 200:
                        reasons = {
                            "40301": "ключ отклонён",
                            "40302": "срок доступа истёк",
                            "40303": "IP не разрешён",
                            "40304": "дневной лимит исчерпан",
                            "40305": "месячный лимит исчерпан",
                        }
                        try:
                            code = str(json.loads(raw).get("error_code", ""))
                        except (ValueError, AttributeError):
                            code = ""
                        if code in reasons:
                            raise AgentError(f"Parser API: {reasons[code]} ({code})")
                        raise AgentError(f"Parser API: HTTP {response.status_code}")
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("done", data.get("Success")) != 1:
                raise AgentError("Источник не выполнил запрос; результат поиска неизвестен")
            temporary = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            temporary.replace(target)
            return data
        except (httpx.TimeoutException, httpx.NetworkError):
            raise TransientSourceError(0) from None
        except (httpx.HTTPError, ValueError, OSError):
            raise AgentError("Не удалось прочитать ответ Parser API") from None

    async def diagnose(self):
        results = {}
        for name, endpoint, params in (
            ("kad", "arbitr_api/details_by_number", {"CaseNumber": "А70-7129/2024"}),
            ("ras", "ras_arbitr_api/search", {"text": "маркетплейс фотографии", "page": 1}),
        ):
            try:
                await self.request(endpoint, params, fresh=True)
                results[name] = {"ok": True, "message": "Источник выполнил запрос"}
            except AgentError as exc:
                results[name] = {"ok": False, "message": str(exc)}
        return results

    async def get_case_card(self, number):
        """Fetch the validated upstream card. Failures remain distinct from an empty list."""
        number = number.strip().upper().replace("A", "А")
        if not re.fullmatch(r"А\d{1,3}-\d{1,8}/\d{4}", number):
            raise AgentError("Введите номер арбитражного дела, например А70-7129/2024")
        data = await self.request("arbitr_api/details_by_number", {"CaseNumber": number})
        rows = data.get("Cases")
        if not isinstance(rows, list):
            raise AgentError("КАД вернул некорректную карточку дела")
        return {"number": number, "rows": rows}

    def list_case_acts(self, card):
        """Convert a retrieved card into separate acts; the returned history is not complete."""
        number, rows = card["number"], card["rows"]
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or row.get("CaseNumber") != number:
                continue
            instances = row.get("CaseInstances")
            if instances is None:
                instances = []
            if not isinstance(instances, list):
                raise AgentError("КАД вернул некорректную карточку дела")
            for instance in instances:
                if not isinstance(instance, dict):
                    raise AgentError("КАД вернул некорректную карточку дела")
                file = instance.get("File") or {}
                court = instance.get("Court") or {}
                if not isinstance(file, dict) or not isinstance(court, dict):
                    raise AgentError("КАД вернул некорректную карточку дела")
                documents = [(file.get("URL", ""), str(file.get("Date") or ""),
                              file.get("Name", "Судебный акт"), "")]
                events = instance.get("InstanceEvents") or []
                if not isinstance(events, list):
                    raise AgentError("КАД вернул некорректную историю событий")
                for event in events:
                    if not isinstance(event, dict):
                        raise AgentError("КАД вернул некорректную историю событий")
                    url = event.get("File", "")
                    if isinstance(url, dict):
                        url = url.get("URL", "")
                    documents.append((url, str(event.get("PublishDate") or event.get("Date") or ""),
                                      str(event.get("EventTypeName") or "Судебный акт"),
                                      str(event.get("EventContentTypeName") or "")))
                for url, date, kind, effect in documents:
                    if not court_url(url) or url in seen:
                        continue
                    seen.add(url)
                    result.append(source_act(
                        number, url, court=court.get("Name", "Не указан"), date=date, kind=kind,
                        instance=instance_name(instance.get("Name", "")), source="kad",
                        case_url="https://kad.arbitr.ru/Card/" + str(row.get("CaseId", "")),
                        effect=effect,
                    ))
        return result

    async def case_by_number(self, number):
        """Compatibility wrapper for the original search/API contract."""
        return self.list_case_acts(await self.get_case_card(number))

    async def search(self, query):
        if re.fullmatch(r"[АаAa]\d{1,3}-\d{1,8}/\d{4}", query.strip()):
            cases = await self.case_by_number(query)
            return cases, {"source": "kad", "lookup_by_number": True, "fetched_pages": 1}
        return await self.search_pages({"text": query})

    async def search_pages(self, params, max_pages=None):
        result, seen, fetched, total_pages, total = [], set(), 0, None, None
        for page in range(1, (max_pages or self.settings.source_max_pages) + 1):
            try:
                data = await self.request("ras_arbitr_api/search", {**params, "page": page})
                rows = self.ras_acts(data)
            except AgentError:
                if not fetched:
                    raise
                return result, {"fetched_pages": fetched, "pages": total_pages,
                                "pagination_complete": False, "partial_failure": True}
            fetched += 1
            total_pages, total = data.get("pages"), data.get("count")
            for act in rows:
                if act["file_url"] not in seen:
                    seen.add(act["file_url"])
                    result.append(act)
            if not rows or total_pages is None or (
                    isinstance(total_pages, int) and page >= total_pages):
                break
        return result, {"total": total, "pages": total_pages, "fetched_pages": fetched,
                        "pagination_complete": isinstance(total_pages, int) and
                        fetched >= total_pages, "partial_failure": False}

    def ras_acts(self, data):
        items = data.get("items")
        if not isinstance(items, list):
            raise AgentError("Источник вернул некорректную поисковую выдачу")
        result, seen = [], set()
        for item in items:
            if not isinstance(item, dict):
                continue
            row = {k.lower(): v for k, v in item.items()}
            number = row.get("casenumber", "")
            url = row.get("fileurl", "")
            if not isinstance(number, str) or not number or url in seen or not court_url(url):
                continue
            seen.add(url)
            result.append(source_act(
                number, url, court=str(row.get("court", "")),
                date=str(row.get("registrationdate", "")), kind=str(row.get("type", "")),
                instance={1: "first", 2: "appeal", 3: "cassation", 4: "supreme"}.get(
                    row.get("instancelevel"), "unknown"),
                case_url=row.get("caseurl", "") if court_url(row.get("caseurl", "")) else "",
                effect="; ".join(str(x) for x in row.get("contenttypes", [])
                                 if isinstance(x, str)),
            ))
        if items and not result:
            raise AgentError("Источник не вернул пригодных судебных актов")
        return result

    async def get_case_history(self, number):
        number = number.strip().upper().replace("A", "А")
        acts, warnings, kad_ok, ras_complete = [], [], False, False
        try:
            acts = await self.case_by_number(number)
            kad_ok = bool(acts)
        except AgentError as exc:
            warnings.append(str(exc))
        try:
            ras, coverage = await self.search_pages({"caseNumber": number},
                                                     max_pages=self.settings.history_max_pages)
            acts.extend(a for a in ras if a["number"].upper().replace("A", "А") ==
                        number.upper().replace("A", "А"))
            ras_complete = coverage["pagination_complete"] and not coverage["partial_failure"]
        except AgentError as exc:
            warnings.append(str(exc))
        merged = {}
        for act in acts:
            prior = merged.get(act["file_url"])
            if prior:
                for key in ("date", "source_effect"):
                    if not prior.get(key):
                        prior[key] = act.get(key, "")
                if prior.get("instance") == "unknown":
                    prior["instance"] = act["instance"]
            else:
                merged[act["file_url"]] = act
        ranks = {"first": 1, "appeal": 2, "cassation": 3, "supreme": 4, "unknown": 5}
        ordered = sorted(merged.values(), key=lambda a: (
            ranks[a["instance"]], date_key(a["date"]), a["id"]))
        truncated = len(ordered) > self.settings.history_max_acts
        ordered = ordered[:self.settings.history_max_acts]
        complete = kad_ok and ras_complete and not truncated
        links = []
        for act in ordered:
            act["number"] = number
            if act["instance"] == "unknown":
                continue
            prior = [a for a in ordered if ranks[a["instance"]] == ranks[act["instance"]] - 1]
            if prior:
                links.append({"from_act_id": prior[-1]["id"], "to_act_id": act["id"],
                              "relation": "same_case_instance_order_not_verified_appeal_target",
                              "effect": act["source_effect"]})
        history = {"number": number, "history_complete": complete,
                   "scope": "published_acts_from_kad_and_ras_at_checked_at",
                   "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "legal_finality_verified": False, "links": links,
                   "source_cache_max_age_seconds": 86400,
                   "act_ids": [a["id"] for a in ordered], "warnings": warnings,
                   "truncated": truncated}
        for act in ordered:
            act["history"] = {k: history[k] for k in (
                "history_complete", "scope", "checked_at", "legal_finality_verified", "truncated")}
            if act["source_effect"]:
                act["appeal_status"] = "Сведения источника: " + act["source_effect"]
        return ordered[:self.settings.history_max_acts], history

    async def download_document(self, act):
        """Download a registered act's document through the existing bounded/cache path."""
        if not court_url(act["file_url"]):
            raise AgentError("Недопустимый адрес судебного акта")
        endpoint = "arbitr_api" if act.get("source") == "kad" else "ras_arbitr_api"
        data = await self.request(endpoint + "/pdf_download", {"url": act["file_url"]})
        try:
            raw = base64.b64decode(data.get("pdfContent") or "", validate=True)
            if not raw.startswith(b"%PDF") or len(raw) > 10_000_000:
                raise ValueError()
        except (ValueError, TypeError):
            raise AgentError("Источник не вернул пригодный PDF") from None
        act["document_sha256"] = self.originals.put(raw)
        act["original_retained"] = True
        act["retrieved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return raw

    async def document_pages(self, raw):
        self.page_quality = []
        try:
            if self.extractor is not None:
                return await asyncio.to_thread(self.extractor, raw)
            digest = self.originals.put(raw)
            result = await extract_document(self.originals.get(digest), self.settings)
            self.page_quality = result["page_quality"]
            return result["pages"]
        except (ValueError, TypeError):
            raise AgentError("Источник не вернул пригодный PDF") from None

    async def read_act(self, case):
        """Legacy result shape with additive, persisted document metadata on the act."""
        raw = await self.download_document(case)
        pages = await self.document_pages(raw)
        case["document_sha256"] = hashlib.sha256(raw).hexdigest()
        case["retrieved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        case["page_quality"] = self.page_quality
        origins = {q["origin"] for q in self.page_quality if q["origin"] != "unknown"}
        case["text_origin"] = ("mixed" if len(origins) > 1 else next(iter(origins), "pdf_text"))
        empty_pages = [p["page"] for p in pages if not p["text"].strip()]
        possibly_truncated = [p["page"] for p in pages if len(p["text"]) >= 20000]
        case["extraction_quality"] = {
            "status": "partial" if empty_pages or possibly_truncated else "text_extracted",
            "empty_pages": empty_pages,
            "possibly_truncated_pages": possibly_truncated,
            "quality_assessed": False,
            "ocr_used": "ocr" in origins,
        }
        expected = re.sub(r"\s+", "", str(case.get("number", "")).upper().replace("A", "А"))
        body = re.sub(r"\s+", "", " ".join(p["text"] for p in pages).upper().replace("A", "А"))
        numbers = set(re.findall(r"А\d{1,3}-\d{1,8}/\d{4}", body))
        case["extraction_quality"]["case_number_status"] = (
            "matched" if expected and expected in numbers else "mismatch" if numbers and expected
            else "unassessed")
        if not any(page["text"].strip() for page in pages):
            raise AgentError("В PDF нет доступного текста: OCR недоступен или не распознал "
                             "страницы; оригинал сохранён, требуется проверка")
        return pages


def extract_pages(raw):
    try:
        reader = PdfReader(io.BytesIO(raw))
        if len(reader.pages) > 150:
            raise ValueError()
        return [
            {"page": index + 1, "text": page.extract_text()[:20000]}
            for index, page in enumerate(reader.pages)
        ]
    except Exception:
        raise ValueError("PDF extraction failed") from None
