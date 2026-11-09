"""Apply a named human review; never auto-confirm provisional labels."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.retrieval_eval import load_tasks  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=ROOT / "eval/retrieval/tasks_v1.jsonl")
    parser.add_argument(
        "--reviews", type=Path, required=True, help="Human-created JSON: task id -> {labels, note}"
    )
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        tasks, _ = load_tasks(args.corpus)
        reviews = json.loads(args.reviews.read_text(encoding="utf-8"))
        if not args.reviewer.strip() or not isinstance(reviews, dict) or not reviews:
            raise ValueError("Missing human review")
        if set(reviews) - {t.id for t in tasks}:
            raise ValueError("Unknown tasks in review")
        for task in tasks:
            if task.id in reviews:
                review = reviews[task.id]
                if not isinstance(review.get("note"), str) or len(review["note"].strip()) < 10:
                    raise ValueError("Human review must explain the relevance judgments")
                labels = review["labels"]
                if set(labels) != set(task.labels) or any(
                    isinstance(v, bool) or v not in (0, 1, 2) for v in labels.values()
                ):
                    raise ValueError("Review must explicitly judge every pooled case")
                task.labels = labels
                task.annotation.status = "confirmed"
                task.annotation.reviewer = args.reviewer.strip()
                task.annotation.reviewed_at = datetime.now(timezone.utc).isoformat()
                task.annotation.note = review.get("note", "")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("".join(t.model_dump_json() + "\n" for t in tasks), encoding="utf-8")
        load_tasks(args.out)
    except (KeyError, OSError, ValueError, TypeError):
        print(
            "Разметка не применена: проверьте идентификаторы, оценки и имя проверяющего.",
            file=sys.stderr,
        )
        return 2
    print("Сохранён корпус с явно указанным автором ручных оценок.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
