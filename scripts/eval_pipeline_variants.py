"""Compare lexical retrieval, no-critic and full model pipelines on a frozen pool."""

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent import AgentError  # noqa: E402
from app.agents.semantic_critic import SemanticCritic  # noqa: E402
from app.agents.tool_research import ResearchAgent  # noqa: E402
from app.budget import BudgetedProvider  # noqa: E402
from app.config import Settings  # noqa: E402
from app.contracts import ReviewResult, case_key  # noqa: E402
from app.gigachat import GigaChatProvider  # noqa: E402
from app.pipeline import SearchPipeline  # noqa: E402
from app.retrieval_eval import (  # noqa: E402
    historical_acts,
    lexical_rank,
    load_tasks,
    rank_metrics,
    unique_cases,
)


class FrozenSource:
    def __init__(self, acts):
        self.acts = acts

    async def search(self, query):
        return lexical_rank(self.acts, query)[:8], {"source": "frozen_pool"}


class NoCritic:
    async def review(self, request):
        return ReviewResult(
            verdict="acceptable", concerns=["Критик отключён для сравнения вариантов"]
        )


async def run(args):
    tasks, fingerprint = load_tasks(args.corpus)
    selected = [t for t in tasks if t.split == args.split][: args.limit]
    if not selected or (
        not args.allow_draft and any(t.annotation.status != "confirmed" for t in selected)
    ):
        raise ValueError("Empty or unreviewed split")
    settings = Settings.from_env()
    if not settings.gigachat_auth_key:
        raise ValueError("GigaChat key is not configured")
    pool = json.loads(args.acts.read_text(encoding="utf-8")) if args.acts else historical_acts(ROOT)
    if isinstance(pool, dict):
        pool = pool["acts"]
    if any(set(t.labels) != {case_key(a["number"]) for a in pool} for t in selected):
        raise ValueError("Every pooled case needs a relevance judgment")
    results = []
    for task in selected:
        ranking = unique_cases(lexical_rank(pool, task.query))
        results.append(
            {
                "task_id": task.id,
                "variant": "lexical",
                "model_calls": 0,
                **rank_metrics(ranking, task.labels),
            }
        )
        for variant in ("without_critic", "full"):
            budget, state, started = {}, {}, time.perf_counter()
            provider = BudgetedProvider(
                GigaChatProvider(settings), budget, settings.total_model_calls, lambda: None
            )
            critic = SemanticCritic(provider) if variant == "full" else NoCritic()
            pipeline = SearchPipeline(
                settings,
                source=FrozenSource(pool),
                provider=provider,
                research_agent=ResearchAgent(provider),
                critic_agent=critic,
            )
            intake = {
                "facts": [],
                "fields": {
                    "platform": [{"value": task.query}],
                    "object": [{"value": "фотографии/инфографика"}],
                    "client_scenario": [{"value": task.scenario}],
                },
            }
            try:
                result = await asyncio.wait_for(
                    pipeline.run(
                        {"mode": "gigachat", "role": "unknown", "search_query": task.query},
                        intake,
                        lambda *a: None,
                        state,
                    ),
                    timeout=settings.run_timeout,
                )
                scores = {
                    c["case_id"]: {"high": 3, "medium": 2, "low": 1}.get(c.get("relevance"), 0)
                    for c in result.get("comparisons", [])
                }
                ranking = unique_cases(
                    sorted(result.get("cases", []), key=lambda a: -scores.get(a["id"], 0))
                )
                error = False
            except (AgentError, asyncio.TimeoutError):
                result, error = state, True
                ranking = unique_cases(state.get("cases", []))
            results.append(
                {
                    "task_id": task.id,
                    "variant": variant,
                    "elapsed_seconds": time.perf_counter() - started,
                    "execution_budget": budget,
                    "partial_failure": error,
                    "critique": result.get("critique", {}),
                    "comparisons": result.get("comparisons", []),
                    "evidence": result.get("evidence", []),
                    "report": result.get("report", {}),
                    **rank_metrics(ranking, task.labels),
                }
            )
    report = {
        "corpus_sha256_lf": fingerprint,
        "model": settings.gigachat_model,
        "expert_confirmed": all(t.annotation.status == "confirmed" for t in selected),
        "prompt_code_sha256": hashlib.sha256(
            b"".join(
                (ROOT / p).read_bytes()
                for p in [
                    "app/agents/tool_research.py",
                    "app/agents/semantic_critic.py",
                    "app/agents/comparison.py",
                    "app/agents/report.py",
                ]
            )
        ).hexdigest(),
        "results": results,
        "limitations": [
            "Исторический фиксированный пул; актуальность оригиналов требует проверки.",
            "Числа retrieval не оценивают смысл отчёта: его отдельно размечает участник 5.",
            "Различия отдельных прогонов модели не являются статистическим доказательством.",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=ROOT / "eval/retrieval/tasks_v1.jsonl")
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument(
        "--acts", type=Path, help="Optional frozen act array or corpus-build manifest"
    )
    parser.add_argument("--limit", type=int, choices=range(1, 41), default=1)
    parser.add_argument("--allow-draft", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "data/eval/pipeline-variants.json")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except (ValueError, OSError):
        print(
            "Прогон не запущен: проверьте ключ, корпус и статус экспертной разметки.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
