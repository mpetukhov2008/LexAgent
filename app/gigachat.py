"""LangChain GigaChat adapter and isolated OAuth diagnostics with verified TLS."""

import asyncio
import json
import ssl
import time
import uuid

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_gigachat.chat_models import GigaChat
from langsmith import tracing_context

from app.agent import TOOLS, AgentError
from app.config import ROOT


def inline_schema(schema):
    definitions = schema.get("$defs", {})

    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            return visit(definitions[value["$ref"].split("/")[-1]])
        return {
            k: visit(v)
            for k, v in value.items()
            if k not in {"$defs", "title", "additionalProperties"}
        }

    return visit(schema)


class GigaChatProvider:
    TOKEN_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    BASE_URL = "https://gigachat.devices.sberbank.ru/api/v1"

    def __init__(self, settings, transport=None, model=None):
        self.settings = settings
        self.transport = transport
        self.token = ""
        self.expires_at = 0
        self.lock = asyncio.Lock()
        self.chat_model = model

    async def request(self, method, url, **kwargs):
        try:
            context = ssl.create_default_context()
            local_ca = ROOT / "data/gigachat-ca.pem"
            if local_ca.exists():
                context.load_verify_locations(str(local_ca))
            async with httpx.AsyncClient(
                verify=context,
                timeout=60,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await client.request(method, url, **kwargs)
            if response.status_code != 200:
                raise AgentError(
                    f"GigaChat: HTTP {response.status_code}. Проверьте доступ и квоту."
                )
            if len(response.content) > 2_000_000:
                raise AgentError("Слишком большой ответ GigaChat")
            return response.json()
        except (httpx.HTTPError, ValueError, OSError):
            raise AgentError(
                "Не удалось получить ответ GigaChat; проверьте сеть и сертификаты"
            ) from None

    async def access_token(self):
        async with self.lock:
            if self.token and self.expires_at > time.time() + 60:
                return self.token
            if not self.settings.gigachat_auth_key:
                raise AgentError("GigaChat не настроен")
            data = await self.request(
                "POST",
                self.TOKEN_URL,
                headers={
                    "Authorization": f"Basic {self.settings.gigachat_auth_key}",
                    "RqUID": str(uuid.uuid4()),
                },
                data={"scope": self.settings.gigachat_scope},
            )
            try:
                self.token = data["access_token"]
                self.expires_at = float(data["expires_at"]) / 1000
            except (KeyError, TypeError, ValueError):
                raise AgentError("Некорректный ответ авторизации GigaChat") from None
            return self.token

    def get_model(self):
        if self.chat_model is None:
            context = ssl.create_default_context()
            local_ca = ROOT / "data/gigachat-ca.pem"
            if local_ca.exists():
                context.load_verify_locations(str(local_ca))
            self.chat_model = GigaChat(
                credentials=self.settings.gigachat_auth_key,
                scope=self.settings.gigachat_scope,
                model=self.settings.gigachat_model,
                base_url=self.BASE_URL + "/",
                ssl_context=context,
                verify_ssl_certs=True,
                temperature=0.1,
                max_tokens=3000,
                timeout=60,
                max_retries=1,
                streaming=False,
                verbose=False,
            )
        return self.chat_model

    async def choose(self, messages, tools=None):
        """LangChain owns model calls; application owns validation and checkpoints."""
        available = tools or TOOLS
        normalized_tools = [
            {
                "type": "function",
                "function": {
                    **tool["function"],
                    "parameters": inline_schema(tool["function"]["parameters"]),
                },
            }
            for tool in available
        ]
        converted, names = [], {}
        for message in messages:
            role, content = message["role"], message.get("content") or ""
            if role == "system":
                converted.append(SystemMessage(content=content))
            elif role == "user":
                converted.append(HumanMessage(content=content))
            elif role == "assistant":
                calls = []
                for call in message.get("tool_calls", []):
                    names[call["id"]] = call["function"]["name"]
                    calls.append(
                        {
                            "id": call["id"],
                            "name": call["function"]["name"],
                            "args": json.loads(call["function"]["arguments"]),
                        }
                    )
                extra = (
                    {"functions_state_id": message["functions_state_id"]}
                    if message.get("functions_state_id")
                    else {}
                )
                converted.append(
                    AIMessage(content=content, tool_calls=calls, additional_kwargs=extra)
                )
            elif role == "tool":
                converted.append(
                    ToolMessage(
                        content=content,
                        tool_call_id=message["tool_call_id"],
                        name=names[message["tool_call_id"]],
                    )
                )
        try:
            choice = (
                normalized_tools[0]["function"]["name"] if len(normalized_tools) == 1 else "auto"
            )
            bound = self.get_model().bind_tools(normalized_tools, tool_choice=choice)
            # Document text is never sent to external observability services.
            with tracing_context(enabled=False):
                response = await bound.ainvoke(converted, config={"callbacks": []})
            if response.response_metadata.get("finish_reason") == "length":
                raise AgentError("Ответ GigaChat обрезан лимитом токенов")
            clean = {
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call["args"], ensure_ascii=False),
                        },
                    }
                    for call in response.tool_calls
                ],
            }
            state_id = response.additional_kwargs.get("functions_state_id")
            if state_id:
                clean["functions_state_id"] = state_id
            usage = response.usage_metadata or {}
            return clean, {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
            }
        except AgentError:
            raise
        except Exception:
            # SDK exceptions may contain URLs, token-bearing headers or request text.
            raise AgentError(
                "GigaChat / LangChain: запрос не выполнен. Проверьте доступ, квоту и сеть."
            ) from None
