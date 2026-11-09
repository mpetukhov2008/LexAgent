import copy
from pathlib import Path

import pytest

from app.contracts import case_key
from app.retrieval_eval import RetrievalTask, evaluate, historical_acts, load_tasks, rank_metrics

ROOT = Path(__file__).resolve().parents[1]


def test_precision_recall_are_case_level_and_do_not_double_count_siblings():
    result = rank_metrics(["a", "a", "b", "c", "d", "e"], {"a": 2, "e": 1, "b": 0})
    assert result["precision_at_5"] == 2 / 5 and result["recall_at_20"] == 1
    result = rank_metrics(["a"], {"a": 2, "e": 1})
    assert result["precision_at_5"] == 0.2 and result["recall_at_20"] == 0.5
    assert rank_metrics([], {"a": 0})["recall_at_20"] is None


def test_draft_corpus_has_40_tasks_and_never_claims_expert_quality():
    tasks, digest = load_tasks(ROOT / "eval/retrieval/tasks_v1.jsonl")
    assert len(tasks) == 40 and len(digest) == 64
    assert all(t.annotation.status == "pending" for t in tasks)
    assert all(t.annotation.assigned_to == "participant_5" for t in tasks)
    acts = historical_acts(ROOT)
    with pytest.raises(ValueError, match="Expert review"):
        evaluate(tasks, acts)
    report = evaluate(tasks, acts, allow_draft=True)
    assert not report["expert_confirmed"] and report["pool_cases"] == 3
    assert set(report["variants"]) == {"lexical", "multi_query_fusion"}


def test_expert_labels_require_name_time_and_explicit_integer_grades():
    task = load_tasks(ROOT / "eval/retrieval/tasks_v1.jsonl")[0][0].model_dump()
    task["annotation"]["status"] = "confirmed"
    with pytest.raises(ValueError):
        RetrievalTask.model_validate(task)
    task["annotation"].update(reviewer="Human reviewer", reviewed_at="2026-11-09T20:00:00Z")
    assert RetrievalTask.model_validate(task).annotation.status == "confirmed"
    task["labels"][next(iter(task["labels"]))] = True
    with pytest.raises(ValueError):
        RetrievalTask.model_validate(task)


def test_case_split_leaks_and_unjudged_pool_are_rejected(tmp_path):
    tasks, _ = load_tasks(ROOT / "eval/retrieval/tasks_v1.jsonl")
    duplicate = copy.deepcopy(tasks[0])
    duplicate.id = "another-id"
    duplicate.split = "test"
    path = tmp_path / "tasks.jsonl"
    path.write_text(
        tasks[0].model_dump_json() + "\n" + duplicate.model_dump_json(), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="leakage"):
        load_tasks(path)
    acts = historical_acts(ROOT)
    tasks[0].labels.pop(case_key(acts[0]["number"]))
    with pytest.raises(ValueError, match="judgment"):
        evaluate([tasks[0]], acts, split=tasks[0].split, allow_draft=True)
