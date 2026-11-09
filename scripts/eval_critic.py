"""Offline evaluation of critic issue detection, not retrieval or legal quality."""

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, field_validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agents.critic import ISSUE_REASONS, CriticAgent, issue_reason  # noqa: E402
from app.contracts import ReviewRequest  # noqa: E402
from app.schemas import StrictModel  # noqa: E402


class CorpusError(ValueError):
    pass


class ExpectedIssue(StrictModel):
    reason: str
    target: Literal["search", "research", "comparison", "report"]
    evidence_ids: list[str] = Field(default_factory=list)
    blocking: bool

    @field_validator("reason")
    @classmethod
    def known_reason(cls, value):
        if value not in ISSUE_REASONS:
            raise ValueError("Unknown critic issue reason")
        return value


class GoldLabels(StrictModel):
    expected_verdict: Literal["acceptable", "revise", "insufficient"]
    expected_issues: list[ExpectedIssue]


class CorpusItem(StrictModel):
    situation_id: str = Field(min_length=1, max_length=100)
    split: Literal["dev", "test"]
    case_groups: list[str] = Field(min_length=1)
    synthetic: bool
    expert_confirmed: bool
    request: ReviewRequest
    gold_labels: GoldLabels


def signature(reason, target, evidence_ids, blocking):
    return reason, target, tuple(sorted(set(evidence_ids))), blocking


def load_corpus(path):
    with path.open("rb") as stream:
        raw = stream.read(2_000_001)
    if len(raw) > 2_000_000:
        raise CorpusError("Корпус превышает лимит 2 МБ")
    items, identifiers, groups, act_splits = [], set(), {}, {}
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = CorpusItem.model_validate_json(line)
        except ValidationError:
            raise CorpusError(f"Строка {line_number}: корпус не соответствует схеме") from None
        if item.situation_id in identifiers:
            raise CorpusError(f"Строка {line_number}: повтор situation_id")
        identifiers.add(item.situation_id)
        if any(e.case_key not in item.case_groups for e in item.request.evidence):
            raise CorpusError(f"Строка {line_number}: дело Evidence не входит в case_groups")
        for group in item.case_groups:
            if group in groups and groups[group] != item.split:
                raise CorpusError("Акты одного дела попали в разные выборки")
            groups[group] = item.split
        act_ids = {e.act_id for e in item.request.evidence}
        act_ids.update(c.case_id for c in item.request.comparisons)
        for aid in act_ids:
            if aid in act_splits and act_splits[aid] != item.split:
                raise CorpusError("Один акт попал в разные выборки")
            act_splits[aid] = item.split
        items.append(item)
    if not items:
        raise CorpusError("Корпус пуст")
    # File fingerprint is independent of Git's Windows CRLF checkout setting.
    return items, hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


async def evaluate(path, split):
    corpus, fingerprint = load_corpus(path)
    items = [item for item in corpus if item.split == split]
    if not items:
        raise CorpusError("Выбранная выборка пуста")
    agent, tp, fp, fn, verdict_hits, results = CriticAgent(), 0, 0, 0, 0, []
    for item in items:
        # Labels and split metadata are never passed to the critic.
        result = await agent.review(item.request)
        expected = Counter(signature(i.reason, i.target, i.evidence_ids, i.blocking)
                           for i in item.gold_labels.expected_issues)
        actual = Counter(signature(issue_reason(i), i.target, i.evidence_ids, i.blocking)
                         for i in result.issues)
        tp += sum((expected & actual).values())
        fp += sum((actual - expected).values())
        fn += sum((expected - actual).values())
        matched = result.verdict == item.gold_labels.expected_verdict
        verdict_hits += matched
        results.append({"situation_id": item.situation_id, "verdict": result.verdict,
                        "expected_verdict": item.gold_labels.expected_verdict,
                        "issues_match": actual == expected, "verdict_match": matched})
    return {
        "evaluation_version": "critic-structural-v1", "contract_version": "2.0",
        "critic_mode": "deterministic_no_model_calls", "corpus_sha256_lf": fingerprint,
        "split": split, "corpus_size": len(items),
        "synthetic_cases": sum(i.synthetic for i in items),
        "expert_confirmed_cases": sum(i.expert_confirmed for i in items),
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "tp": tp, "fp": fp, "fn": fn, "verdict_accuracy": verdict_hits / len(items),
        "all_labels_match": all(r["issues_match"] and r["verdict_match"] for r in results),
        "results": results,
        "limitations": [
            "Измеряется детекция заданных структурных ошибок, а не юридическая точность.",
            "Набор синтетических регрессий не заменяет экспертную слепую test-выборку.",
            "Проверка атрибуции ограничена явной формулировкой и переданными типами цитат.",
            "Precision@5, Recall@20, качество поиска и смысл цитат не измеряются.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=ROOT / "eval/critic/corpus_v1.jsonl")
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--out", type=Path, default=ROOT / "reports/critic-eval.json")
    parser.add_argument("--check", action="store_true", help="Fail if any expected label differs")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(evaluate(args.corpus, args.split))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    except (CorpusError, OSError, UnicodeError):
        print("Eval не выполнен: проверьте схему, файл корпуса и разделение дел.", file=sys.stderr)
        return 2
    print(json.dumps({k: report[k] for k in ("split", "corpus_size", "tp", "fp", "fn",
                                           "precision", "recall", "verdict_accuracy")},
                     ensure_ascii=False))
    return 1 if args.check and not report["all_labels_match"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
