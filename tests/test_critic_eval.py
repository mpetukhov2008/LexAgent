import asyncio
import copy
import json
from pathlib import Path

import pytest

from scripts.eval_critic import CorpusError, evaluate, load_corpus, main

CORPUS = Path(__file__).resolve().parents[1] / "eval/critic/corpus_v1.jsonl"


def write_rows(tmp_path, rows):
    path = tmp_path / "corpus.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")
    return path


def rows():
    return [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("split,size", [("dev", 10), ("test", 8)])
def test_corpus_labels_and_contract_are_reproducible(split, size):
    result = asyncio.run(evaluate(CORPUS, split))
    assert result["corpus_size"] == size and result["all_labels_match"]
    assert result["expert_confirmed_cases"] == 0 and result["synthetic_cases"] == size
    assert result["fp"] == result["fn"] == 0 and result["verdict_accuracy"] == 1


@pytest.mark.parametrize("kind", ["case", "act", "duplicate", "schema", "group"])
def test_invalid_corpus_and_split_leaks_fail_even_when_evaluating_dev(tmp_path, kind):
    items = rows()
    if kind == "case":
        items[-1]["case_groups"] = items[0]["case_groups"]
        items[-1]["request"]["evidence"][0]["case_key"] = items[0]["case_groups"][0]
    if kind == "act":
        items[-1]["request"]["comparisons"][0]["case_id"] = (
            items[0]["request"]["comparisons"][0]["case_id"])
    if kind == "duplicate":
        items[-1]["situation_id"] = items[0]["situation_id"]
    if kind == "schema":
        items[0]["request"]["comparisons"][0]["findings"][0]["statement_type"] = "court_finding"
    if kind == "group":
        items[0]["case_groups"] = ["different-case"]
    with pytest.raises(CorpusError):
        asyncio.run(evaluate(write_rows(tmp_path, items), "dev"))


def test_empty_split_or_empty_corpus_never_produces_perfect_metrics(tmp_path):
    with pytest.raises(CorpusError):
        asyncio.run(evaluate(write_rows(tmp_path, []), "dev"))
    with pytest.raises(CorpusError):
        asyncio.run(evaluate(write_rows(tmp_path, rows()[:1]), "test"))


def test_no_positive_labels_has_undefined_precision_recall(tmp_path):
    result = asyncio.run(evaluate(write_rows(tmp_path, [rows()[1]]), "dev"))
    assert result["precision"] is None and result["recall"] is None
    assert result["all_labels_match"]


def test_eval_scores_target_and_evidence_membership_and_check_can_fail(tmp_path):
    item = copy.deepcopy(rows()[3])
    item["gold_labels"]["expected_issues"][0]["evidence_ids"] = ["wrong-id"]
    path = write_rows(tmp_path, [item])
    result = asyncio.run(evaluate(path, "dev"))
    assert result["tp"] == 0 and result["fp"] == result["fn"] == 1
    assert not result["all_labels_match"]
    assert main(["--corpus", str(path), "--check", "--out", str(tmp_path / "out.json")]) == 1


def test_repeated_issue_reasons_are_counted_as_occurrences(tmp_path):
    item = rows()[2]
    second = copy.deepcopy(item["request"]["comparisons"][0]["findings"][0])
    item["request"]["comparisons"][0]["findings"].append(second)
    # One gold error against two independent erroneous findings must leave a false positive.
    result = asyncio.run(evaluate(write_rows(tmp_path, [item]), "dev"))
    assert result["tp"] == 1 and result["fp"] == 1 and result["precision"] == 0.5


def test_fingerprint_is_portable_and_bad_file_exits_nonzero(tmp_path):
    path = tmp_path / "windows.jsonl"
    path.write_bytes(CORPUS.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert load_corpus(path)[1] == load_corpus(CORPUS)[1]
    assert main(["--corpus", str(tmp_path / "missing")]) == 2
