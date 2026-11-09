"""Explicit paid/quota-consuming GigaChat smoke test on synthetic materials only.

Uses an in-memory source substitute, not fabricated live judicial search results.
"""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent import IntakeAgent  # noqa: E402
from app.config import Settings  # noqa: E402
from app.pipeline import SearchPipeline  # noqa: E402
from app.schemas import Intake  # noqa: E402


class SyntheticSource:
    async def search(self, query):
        return [
            {
                "id": "synthetic-1",
                "number": "УЧЕБНЫЙ ПРИМЕР — НЕ СУДЕБНОЕ ДЕЛО",
                "court": "Синтетический источник",
                "date": "",
                "kind": "Тест",
                "file_url": "",
                "case_url": "",
                "appeal_status": "Не применимо: тест",
                "provenance": "synthetic",
                "pages": [
                    {
                        "page": 1,
                        "text": "Учебный материал, не судебный акт. "
                        "В вымышленной ситуации продавец "
                        "самостоятельно разместил фотографии товара на Ozon. Принадлежность "
                        "исключительных прав подтверждалась договором с фотографом. "
                        "Размещение фиксировалось протоколом осмотра. "
                        "Эти предложения используются только для проверки "
                        "инструментов и цитат.",
                    }
                ],
            }
        ], {"source": "synthetic", "total": 1, "pages": 1}


async def main():
    settings = Settings.from_env()
    payload = Intake.model_validate_json(
        (ROOT / "samples/complete.json").read_text(encoding="utf-8")
    ).model_dump()
    # Intake already has a separate live test; prepare a deterministic input contract here.
    intake = await IntakeAgent(payload, settings).run(lambda *args: None)
    payload["mode"] = "gigachat"
    events = []

    def emit(kind, detail):
        events.append({"kind": kind, "detail": detail})
        print(kind, detail.get("stage", ""), detail.get("tool", ""), flush=True)

    result = await SearchPipeline(settings, source=SyntheticSource()).run(payload, intake, emit)
    (ROOT / "reports/gigachat-pipeline-live.json").write_text(
        json.dumps(
            {"synthetic_sources": True, "result": result, "events": events},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("Live baseline agents completed on synthetic source", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
