"""Download a bounded, reproducible case corpus; originals stay outside Git."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent import AgentError  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Store  # noqa: E402
from app.library import Library  # noqa: E402
from app.postgres import PostgresStore  # noqa: E402
from app.sources import ParserClient  # noqa: E402


async def build(numbers, limit, output):
    settings = Settings.from_env()
    source = ParserClient(settings)
    store = (
        PostgresStore(settings.database_url) if settings.database_url else Store(settings.database)
    )
    library, documents, histories, warnings = Library(store), [], {}, []
    for number in numbers:
        acts, history = await source.get_case_history(number)
        histories[number] = history
        for act in acts:
            if len(documents) >= limit:
                warnings.append("Достигнут лимит загруженных документов; история может быть шире")
                break
            try:
                act["pages"] = await source.read_act(act)
            except AgentError as exc:
                act["read_error"] = str(exc)
            if act.get("document_sha256"):
                library.save(act)
                if act["id"] not in {a["id"] for a in documents}:
                    documents.append(act)
        warnings.extend(history["warnings"])
    result = {
        "schema_version": "case-corpus-1",
        "expert_confirmed": False,
        "assigned_reviewer": "participant_5",
        "source_calls": source.calls,
        "histories": histories,
        "acts": documents,
        "warnings": warnings,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"documents": len(documents), "source_calls": source.calls, "expert_confirmed": False},
            ensure_ascii=False,
        )
    )
    return 0 if documents else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", required=True, dest="numbers")
    parser.add_argument("--limit", type=int, choices=range(1, 101), default=8)
    parser.add_argument("--out", type=Path, default=ROOT / "data/corpus/cases.json")
    args = parser.parse_args()
    return asyncio.run(build(args.numbers, args.limit, args.out))


if __name__ == "__main__":
    raise SystemExit(main())
