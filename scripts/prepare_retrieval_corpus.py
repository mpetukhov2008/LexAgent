"""Prepare 40 provisional annotation tasks anchored to the existing historical snapshot."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.contracts import case_key  # noqa: E402
from app.retrieval_eval import historical_acts  # noqa: E402

SCENARIOS = [
    (
        "фотографии авторство исключительные права",
        "Проверить подтверждение прав на фотографию.",
        "прав",
    ),
    (
        "инфографика творческий характер изображения",
        "Проверить применимость к инфографике.",
        "творч",
    ),
    (
        "карточка товара общая карточка продавец размещение",
        "Неизвестно, кто загрузил контент.",
        "размещ",
    ),
    (
        "маркетплейс автоматическое размещение фотографий",
        "Проверить роль продавца и площадки.",
        "маркет",
    ),
    (
        "скриншот фиксация нарушения доказательства",
        "Проверить способ фиксации размещения.",
        "доказ",
    ),
    (
        "договор передача исключительных прав фотографии",
        "Проверить цепочку передачи прав.",
        "договор",
    ),
    (
        "компенсация снижение размер нарушения фотографии",
        "Проверить значимые отличия в требованиях.",
        "компенс",
    ),
    (
        "апелляция отмена решения фотографии",
        "Проверить последующие акты и ограничения окончательности.",
        "суд",
    ),
    (
        "возражения ответчика авторство фотографий",
        "Отделить довод стороны от установленного факта.",
        "ответчик",
    ),
    (
        "фотографии товар отсутствие нарушения доказательства",
        "Найти противоположные выводы и отличия.",
        "истец",
    ),
]


def main():
    acts = sorted(historical_acts(ROOT), key=lambda a: a["number"])
    if len(acts) < 3:
        raise ValueError("Historical snapshot is incomplete")
    groups = {case_key(a["number"]) for a in acts}
    tasks = []
    for index in range(40):
        act = acts[index % len(acts)]
        query, scenario, stem = SCENARIOS[index % len(SCENARIOS)]
        matching = next((p for p in act["pages"] if stem in p["text"].casefold()), act["pages"][0])
        group = case_key(act["number"])
        tasks.append(
            {
                "id": f"retrieval_draft_{index + 1:03d}",
                "split": "test" if act["number"] == acts[-1]["number"] else "dev",
                "query": query + (" Ozon" if index % 2 else " Wildberries"),
                "scenario": "Гипотетическая задача для разметки, не реальная фабула клиента: "
                + scenario,
                "source_case_groups": [group],
                "source_refs": [
                    {
                        "act_id": act["id"],
                        "number": act["number"],
                        "page": matching["page"],
                        "quote": matching["text"][:500],
                        "url": act["file_url"],
                        "provenance": "historical_report_not_revalidated",
                    }
                ],
                "labels": {g: int(g == group) * 2 for g in sorted(groups)},
                "annotation": {
                    "status": "pending",
                    "assigned_to": "participant_5",
                    "reviewer": None,
                    "reviewed_at": None,
                    "note": "Автоматическая предварительная метка по привязке к источнику; "
                    "каждая оценка, включая отрицательную, требует ручной проверки.",
                },
            }
        )
    target = ROOT / "eval/retrieval/tasks_v1.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks), encoding="utf-8"
    )
    print("Подготовлено 40 черновых задач на 3 исторических делах; экспертных меток нет.")


if __name__ == "__main__":
    main()
