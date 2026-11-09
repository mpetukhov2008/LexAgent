"""Participant 1: bounded tool-using intake agent, with an explicit local demo policy."""

import json
import re
from pathlib import Path

import httpx
from pydantic import ValidationError

from app.schemas import (
    LABELS,
    QUESTIONS,
    AskQuestion,
    Empty,
    Finish,
    ReadDocument,
    RecordFacts,
)

PROMPT = (Path(__file__).parent / "prompts/intake.txt").read_text(encoding="utf-8")
TOOL_MODELS = {
    "list_documents": (Empty, "Получить список доступных источников и статус чтения."),
    "read_document": (ReadDocument, "Прочитать источник по ID. Только локальные тексты запроса."),
    "record_facts": (
        RecordFacts,
        "Записать сведения с дословными цитатами прочитанных источников.",
    ),
    "ask_clarification": (AskQuestion, "Запросить уточнение одного поля у юриста."),
    "finish": (Finish, "Завершить разбор. Код проверит пробелы и противоречия."),
}
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": model.model_json_schema(),
        },
    }
    for name, (model, description) in TOOL_MODELS.items()
]


class AgentError(Exception):
    """Only safe, authored messages may cross the API boundary."""


class ToolError(Exception):
    pass


def sources_for(payload):
    sources = {
        "narrative": {
            "name": "Описание клиента",
            "text": payload["narrative"],
            "kind": "client_statement",
        }
    }
    for i, doc in enumerate(payload["documents"], 1):
        sources[f"doc-{i}"] = {**doc, "kind": "document_statement"}
    for key, value in payload["answers"].items():
        sources[f"answer-{key}"] = {
            "name": f"Уточнение: {LABELS[key]}",
            "text": f"{LABELS[key]}: {value}",
            "kind": "client_statement",
        }
    return sources


def normalize(value):
    return re.sub(r"\s+", " ", value.casefold().replace("ё", "е")).strip(" .;:")


def unknown(value):
    value = normalize(value)
    return value in {"?", "-"} or bool(
        re.match(r"^(неизвест\w*|не знаю|нет данных|не указан\w*|не установлен\w*)(\b|$)", value)
    )


def conflicts_for(facts):
    conflicts = []
    # Multiple objects / demands / rights grounds can coexist. Dates can refer to different events.
    for key in ("platform", "placement"):
        values = {normalize(f["value"]) for f in facts if f["field"] == key}
        if len(values) > 1:
            conflicts.append(
                {
                    "field": key,
                    "message": (
                        f"Разные формулировки поля «{LABELS[key]}»: нужна проверка юриста. "
                        "Это возможное противоречие, а не установленная ложность источника."
                    ),
                }
            )
    return conflicts


class IntakeAgent:
    def __init__(self, payload, settings, checkpoint=None, provider=None):
        self.payload = payload
        self.settings = settings
        self.sources = sources_for(payload)
        if provider is not None:
            self.provider = provider
        elif payload["mode"] == "gigachat":
            from app.gigachat import GigaChatProvider

            self.provider = GigaChatProvider(settings)
        else:
            self.provider = YandexProvider(settings)
        self.state = checkpoint or {
            "version": 1,
            "steps": 0,
            "model_calls": 0,
            "read": [],
            "facts": [],
            "questions": {},
            "messages": [],
            "finished": False,
            "listed": False,
            "processed": [],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
        if self.state.get("version") != 1:
            raise AgentError("Версия контрольной точки не поддерживается")

    def execute(self, name, args):
        if name not in TOOL_MODELS:
            raise ToolError("Инструмент не разрешен")
        try:
            args = TOOL_MODELS[name][0].model_validate(args).model_dump()
        except ValidationError:
            raise ToolError("Аргументы не соответствуют контракту инструмента") from None
        if name == "list_documents":
            self.state["listed"] = True
            return [
                {
                    "id": key,
                    "name": source["name"],
                    "kind": source["kind"],
                    "read": key in self.state["read"],
                }
                for key, source in self.sources.items()
            ]
        if name == "read_document":
            sid = args["source_id"]
            if sid not in self.sources:
                raise ToolError("Источник не принадлежит этому запросу")
            if sid not in self.state["read"]:
                self.state["read"].append(sid)
            return {"source_id": sid, **self.sources[sid]}
        if name == "record_facts":
            additions = []
            for fact in args["facts"]:
                sid = fact["source_id"]
                if sid not in self.state["read"]:
                    raise ToolError("Сначала прочитайте источник")
                if fact["quote"] not in self.sources[sid]["text"]:
                    raise ToolError("Цитата не найдена дословно в указанном источнике")
                if unknown(fact["value"]):
                    raise ToolError("Неизвестное значение нельзя сохранять как установленное")
                # Status is assigned by code, never by the model. Documentary support != truth.
                fact["status"] = self.sources[sid]["kind"]
                fact["source_name"] = self.sources[sid]["name"]
                fact["quote_start"] = self.sources[sid]["text"].index(fact["quote"])
                if fact not in self.state["facts"] and fact not in additions:
                    additions.append(fact)
            if len(self.state["facts"]) + len(additions) > 60:
                raise ToolError("Превышен лимит 60 сведений")
            self.state["facts"].extend(additions)
            return {"recorded": len(additions), "conflicts": conflicts_for(self.state["facts"])}
        if name == "ask_clarification":
            self.state["questions"][args["field"]] = args["question"]
            return {"queued_for_user": True, "field": args["field"]}
        unread = set(self.sources) - set(self.state["read"])
        if unread:
            raise ToolError("Перед завершением нужно прочитать все предоставленные источники")
        facts = self.state["facts"]
        known = {f["field"] for f in facts}
        unprocessed = set(LABELS) - known - set(self.state["questions"])
        if self.payload["mode"] != "demo" and unprocessed:
            raise ToolError(
                "Нельзя завершить разбор без обработки полей: "
                + ", ".join(sorted(unprocessed))
                + ". Сохраните явно указанные сведения через record_facts; "
                "только для отсутствующих сведений вызовите ask_clarification."
            )
        for key in LABELS:
            if key not in known:
                self.state["questions"].setdefault(key, QUESTIONS[key])
        conflicts = conflicts_for(facts)
        for conflict in conflicts:
            key = conflict["field"]
            self.state["questions"][key] = (
                f"Уточните поле «{LABELS[key]}»: в источниках разные сведения. "
                "Объясните различие или исправьте исходный текст."
            )
        self.state["finished"] = True
        self.state["reason"] = args["reason"]
        return {"finished": True, "questions": len(self.state["questions"])}

    def available_tools(self):
        """Expose only actions whose prerequisites hold; validation remains mandatory."""
        if not self.state["listed"]:
            names = {"list_documents"}
        elif not self.state["read"]:
            names = {"read_document"}
        else:
            known = {fact["field"] for fact in self.state["facts"]}
            covered = known | set(self.state["questions"])
            unread = set(self.sources) - set(self.state["read"])
            if not unread and covered == set(LABELS):
                names = {"finish"}
            else:
                names = {"record_facts", "ask_clarification"}
                if unread:
                    names.add("read_document")
        return [tool for tool in TOOLS if tool["function"]["name"] in names]

    def demo_decision(self):
        """Transparent rule policy for defense; no simulation of a remote model."""
        state = self.state
        if not state["listed"]:
            return "list_documents", {}, "Определить доступные источники"
        for sid in self.sources:
            if sid not in state["read"]:
                return "read_document", {"source_id": sid}, "Прочитать еще не изученный источник"
            if sid not in state["processed"]:
                state["processed"].append(sid)
                facts = []
                for line in self.sources[sid]["text"].splitlines():
                    for key, label in LABELS.items():
                        match = re.match(rf"\s*{re.escape(label)}\s*:\s*(.+)", line, re.I)
                        if match and not unknown(match[1]):
                            facts.append(
                                {
                                    "field": key,
                                    "value": match[1].strip(),
                                    "source_id": sid,
                                    "quote": line.strip(),
                                }
                            )
                if facts:
                    return (
                        "record_facts",
                        {"facts": facts[:20]},
                        "Сохранить явно указанные сведения",
                    )
        known = {f["field"] for f in state["facts"]}
        for key in LABELS:
            if key not in known and key not in state["questions"]:
                return (
                    "ask_clarification",
                    {"field": key, "question": QUESTIONS[key]},
                    ("В источниках нет однозначного значения — запросить уточнение"),
                )
        for item in conflicts_for(state["facts"]):
            key = item["field"]
            if key not in state["questions"]:
                return (
                    "ask_clarification",
                    {"field": key, "question": item["message"]},
                    ("Сведения расходятся — нужна проверка"),
                )
        return (
            "finish",
            {"reason": "Доступные источники прочитаны, пробелы отмечены"},
            ("Завершить разбор в пределах полномочий агента 1"),
        )

    async def run(self, save):
        while not self.state["finished"]:
            if self.state["steps"] >= self.settings.max_steps:
                raise AgentError("Достигнут лимит действий; результат не считается завершенным")
            model_message = None
            if self.payload["mode"] == "demo":
                name, args, reason = self.demo_decision()
            else:
                if self.state["model_calls"] >= self.settings.max_model_calls:
                    raise AgentError("Достигнут лимит запросов модели")
                self.state["model_calls"] += 1
                save(
                    self.state,
                    "model_request",
                    {"message": "Запрос к языковой модели", "call": self.state["model_calls"]},
                )
                messages = [
                    {"role": "system", "content": PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "title": self.payload["title"],
                                "role": self.payload["role"],
                                "instruction": "Разбери ситуацию разрешенными инструментами.",
                            },
                            ensure_ascii=False,
                        ),
                    },
                ] + self.state["messages"]
                if self.payload["mode"] == "gigachat":
                    model_message, usage = await self.provider.choose(
                        messages, self.available_tools()
                    )
                else:
                    model_message, usage = await self.provider.choose(messages)
                for key in self.state["usage"]:
                    self.state["usage"][key] += int(usage.get(key, 0) or 0)
                calls = model_message.get("tool_calls", [])
                if len(calls) != 1:
                    raise AgentError("Модель должна вернуть один вызов инструмента на шаг")
                try:
                    name = calls[0]["function"]["name"]
                    args = json.loads(calls[0]["function"]["arguments"])
                except (KeyError, TypeError, ValueError):
                    raise AgentError("Некорректный вызов инструмента от модели") from None
                reason = "Модель выбрала следующий инструмент"
            self.state["steps"] += 1
            try:
                output = self.execute(name, args)
                ok = True
            except ToolError as exc:
                output = {"error": str(exc)}
                ok = False
            if model_message:
                self.state["messages"].extend(
                    [
                        model_message,
                        {
                            "role": "tool",
                            "tool_call_id": model_message["tool_calls"][0]["id"],
                            "content": json.dumps(output, ensure_ascii=False),
                        },
                    ]
                )
            save(
                self.state,
                "tool",
                {
                    "step": self.state["steps"],
                    "tool": name,
                    "reason": reason,
                    "source_id": args.get("source_id") if isinstance(args, dict) else None,
                    "field": args.get("field") if isinstance(args, dict) else None,
                    "facts_count": len(self.state["facts"]),
                    "ok": ok,
                    "message": (
                        output.get("error")
                        if isinstance(output, dict) and not ok
                        else "Инструмент выполнен"
                    ),
                },
            )
        return self.result()

    def result(self):
        state = self.state
        return {
            "schema_version": "1.0",
            "agent": "intake",
            "mode": self.payload["mode"],
            "model": (
                self.settings.gigachat_model
                if self.payload["mode"] == "gigachat"
                else self.settings.model
                if self.payload["mode"] == "yandex"
                else None
            ),
            "title": self.payload["title"],
            "role": self.payload["role"],
            "facts": state["facts"],
            "fields": {key: [f for f in state["facts"] if f["field"] == key] for key in LABELS},
            "questions": [{"field": k, "question": v} for k, v in state["questions"].items()],
            "conflicts": conflicts_for(state["facts"]),
            "reason": state["reason"],
            "steps": state["steps"],
            "usage": state["usage"],
            "ready_for_search": not bool(state["questions"]),
            "limits": [
                "Это разбор исходных сведений, а не оценка перспектив и не поиск судебных дел.",
                "Цитата подтверждает наличие слов в источнике, но не достоверность утверждения.",
                "Документы не прошли независимую юридическую проверку.",
            ],
            "handoff": {"next_agent": "search_planner", "implemented": False},
        }


class YandexProvider:
    URL = "https://ai.api.cloud.yandex.net/v1/chat/completions"

    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport

    async def choose(self, messages):
        if not self.settings.yandex_ready:
            raise AgentError("Yandex AI Studio не настроен: проверьте локальный .env")
        try:
            async with httpx.AsyncClient(
                timeout=40, transport=self.transport, follow_redirects=False
            ) as client:
                response = await client.post(
                    self.URL,
                    headers={
                        "Authorization": f"Bearer {self.settings.api_key}",
                        "OpenAI-Project": self.settings.folder_id,
                        "x-data-logging-enabled": "false",
                    },
                    json={
                        "model": self.settings.model,
                        "messages": messages,
                        "tools": TOOLS,
                        "tool_choice": "auto",
                        "temperature": 0.1,
                        "max_tokens": 2500,
                    },
                )
            if response.status_code in (401, 403):
                raise AgentError("Yandex отклонил доступ: проверьте ключ, каталог и роль")
            if response.status_code == 429:
                raise AgentError("Лимит Yandex API; повторите запуск позже")
            if response.status_code != 200:
                raise AgentError(f"Yandex API вернул HTTP {response.status_code}")
            if len(response.content) > 1_000_000:
                raise AgentError("Ответ модели превышает допустимый размер")
            data = response.json()
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise AgentError("Ответ модели обрезан лимитом токенов")
            message = choice["message"]
            clean = {
                "role": "assistant",
                "content": message.get("content"),
                "tool_calls": message.get("tool_calls", []),
            }
            for call in clean["tool_calls"]:
                if not isinstance(call.get("id"), str):
                    raise ValueError("missing tool id")
            return clean, data.get("usage", {})
        except httpx.TimeoutException:
            raise AgentError("Yandex API не ответил за 40 секунд") from None
        except httpx.HTTPError:
            raise AgentError("Не удалось соединиться с Yandex API") from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise AgentError("Yandex API вернул некорректный формат ответа") from None
