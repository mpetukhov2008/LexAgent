"""Bounded, cached Parser API access. No raw upstream errors cross the boundary."""

import asyncio
import base64
import hashlib
import io
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pypdf import PdfReader

from app.agent import AgentError
from app.config import ROOT


def court_url(value):
    try:
        url = urlsplit(value)
        return (
            url.scheme == "https"
            and url.hostname in {"kad.arbitr.ru", "ras.arbitr.ru"}
            and not url.username
        )
    except (ValueError, TypeError):
        return False


class ParserClient:
    def __init__(self, settings, transport=None, cache_dir=None):
        self.settings = settings
        self.transport = transport
        self.cache_dir = Path(cache_dir or ROOT / "data/source-cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.calls = 0

    async def request(self, endpoint, params, *, fresh=False):
        cache_key = hashlib.sha256(
            json.dumps([endpoint, params], sort_keys=True).encode()
        ).hexdigest()
        target = self.cache_dir / (cache_key + ".json")
        if not fresh and target.exists() and time.time() - target.stat().st_mtime < 86400:
            try:
                return json.loads(target.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        if not self.settings.parser_api_key:
            raise AgentError("Parser API не настроен")
        if self.calls >= 8:
            raise AgentError("Достигнут лимит 8 обращений к источнику за запуск")
        self.calls += 1
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
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            temporary.replace(target)
            return data
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

    async def case_by_number(self, number):
        number = number.strip().upper().replace("A", "А")
        if not re.fullmatch(r"А\d{1,3}-\d{1,8}/\d{4}", number):
            raise AgentError("Введите номер арбитражного дела, например А70-7129/2024")
        data = await self.request("arbitr_api/details_by_number", {"CaseNumber": number})
        rows = data.get("Cases")
        if not isinstance(rows, list):
            raise AgentError("КАД вернул некорректную карточку дела")
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or row.get("CaseNumber") != number:
                continue
            for instance in row.get("CaseInstances") or []:
                file = instance.get("File") or {}
                url = file.get("URL", "")
                if not court_url(url) or url in seen:
                    continue
                seen.add(url)
                result.append(
                    {
                        "id": hashlib.sha256(url.encode()).hexdigest()[:16],
                        "number": number,
                        "court": (instance.get("Court") or {}).get("Name", "Не указан"),
                        "date": "",
                        "kind": file.get("Name", "Судебный акт"),
                        "file_url": url,
                        "case_url": "https://kad.arbitr.ru/Card/" + str(row.get("CaseId", "")),
                        "appeal_status": "Цепочка обжалования не проверена",
                        "pages": [],
                        "quotes": [],
                        "source": "kad",
                    }
                )
        return result[:3]

    async def search(self, query):
        if re.fullmatch(r"[АаAa]\d{1,3}-\d{1,8}/\d{4}", query.strip()):
            cases = await self.case_by_number(query)
            return cases, {"source": "kad", "lookup_by_number": True, "fetched_pages": 1}
        data = await self.request("ras_arbitr_api/search", {"text": query, "page": 1})
        items = data.get("items")
        if not isinstance(items, list):
            raise AgentError("Источник вернул некорректную поисковую выдачу")
        result, seen = [], set()
        for item in items:
            if not isinstance(item, dict):
                continue
            row = {k.lower(): v for k, v in item.items()}
            number = str(row.get("casenumber", ""))
            url = row.get("fileurl", "")
            if not number or number in seen or not court_url(url):
                continue
            seen.add(number)
            result.append(
                {
                    "id": hashlib.sha256(url.encode()).hexdigest()[:16],
                    "number": number,
                    "court": str(row.get("court", "")),
                    "date": str(row.get("registrationdate", "")),
                    "kind": str(row.get("type", "")),
                    "file_url": url,
                    "case_url": row.get("caseurl", "") if court_url(row.get("caseurl", "")) else "",
                    "appeal_status": "Не проверен",
                    "pages": [],
                    "quotes": [],
                }
            )
        return result[:10], {
            "total": data.get("count"),
            "pages": data.get("pages"),
            "fetched_pages": 1,
        }

    async def read_act(self, case):
        if not court_url(case["file_url"]):
            raise AgentError("Недопустимый адрес судебного акта")
        endpoint = "arbitr_api" if case.get("source") == "kad" else "ras_arbitr_api"
        data = await self.request(endpoint + "/pdf_download", {"url": case["file_url"]})
        try:
            raw = base64.b64decode(data.get("pdfContent") or "", validate=True)
            if not raw.startswith(b"%PDF") or len(raw) > 10_000_000:
                raise ValueError()
            pages = await asyncio.to_thread(extract_pages, raw)
        except (ValueError, TypeError):
            raise AgentError("Источник не вернул пригодный PDF") from None
        if not any(page["text"].strip() for page in pages):
            raise AgentError("В PDF нет извлекаемого текста; требуется OCR")
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
