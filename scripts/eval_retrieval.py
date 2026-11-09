"""Evaluate case retrieval; expert mode refuses pending annotations."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.retrieval_eval import evaluate, historical_acts, load_tasks  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=ROOT / "eval/retrieval/tasks_v1.jsonl")
    parser.add_argument("--acts", type=Path, help="Optional JSON array of a frozen act pool")
    parser.add_argument("--split", choices=("dev", "test"), default="test")
    parser.add_argument("--allow-draft", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "reports/retrieval-eval.json")
    args = parser.parse_args()
    try:
        tasks, digest = load_tasks(args.corpus)
        acts = (
            json.loads(args.acts.read_text(encoding="utf-8"))
            if args.acts
            else historical_acts(ROOT)
        )
        if isinstance(acts, dict):
            acts = acts["acts"]
        result = evaluate(tasks, acts, split=args.split, allow_draft=args.allow_draft)
        result["corpus_sha256_lf"] = digest
        result["pool_sha256"] = hashlib.sha256(
            json.dumps(acts, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except (OSError, ValueError):
        print(
            "Измерение не выполнено: проверьте корпус, пул, разбиение и экспертные метки.",
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {k: result[k] for k in ("tasks", "pool_cases", "expert_confirmed")}, ensure_ascii=False
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
