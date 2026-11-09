import pytest
from pydantic import ValidationError

from app.contracts import Evidence, case_key, normalize_evidence, registry


def test_contract_groups_acts_without_changing_legacy_ids():
    acts = [
        {"id": "first", "number": "А70-1/2026", "pages": []},
        {"id": "appeal", "number": "A70-1/2026", "pages": []},
    ]
    cases, documents = registry(acts)
    assert len(cases) == 1
    assert cases[0]["act_ids"] == ["first", "appeal"]
    assert {a["id"] for a in documents} == {"first", "appeal"}
    assert not cases[0]["history_complete"]
    assert case_key("А70-1/2026") == case_key("A70-1/2026")


def test_external_verified_flag_does_not_bypass_source_validation():
    acts = [{"id": "a", "number": "А70-1/2026", "pages": [
        {"page": 1, "text": "Суд установил обстоятельства размещения фотографий."}
    ]}]
    evidence, rejected = normalize_evidence([
        {"case_id": "a", "page": 1, "quote": "обстоятельства размещения фотографий"},
        {"case_id": "a", "page": 1, "quote": "Выдуманный вывод суда", "text_verified": True},
    ], acts)
    assert len(evidence) == rejected == 1
    assert evidence[0]["id"].startswith("ev_")
    assert evidence[0]["case_id"] == evidence[0]["act_id"] == "a"
    assert evidence[0]["assertion_type"] == "unknown"
    with pytest.raises(ValidationError):
        Evidence.model_validate({**evidence[0], "act_id": "other"})
