"""Explicit read-only API checks. Credentials and upstream response text are never printed."""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent import AgentError  # noqa: E402
from app.config import Settings  # noqa: E402
from app.gigachat import GigaChatProvider  # noqa: E402
from app.sources import ParserClient  # noqa: E402


async def main():
    settings = Settings.from_env()
    result = {}
    try:
        await GigaChatProvider(settings).access_token()
        result["gigachat"] = "OAuth OK"
    except AgentError as exc:
        result["gigachat"] = str(exc)
    try:
        result["parser"] = await ParserClient(settings).diagnose()
    except AgentError as exc:
        result["parser"] = str(exc)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
