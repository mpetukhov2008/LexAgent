from app.contracts import normalize_evidence
from app.db import Store
from app.materials import Materials


def exercise_materials(store):
    rid, _ = store.create({"title": "Синтетическая проверка", "mode": "demo"}, "materials-test")
    acts = [{"id": "a", "number": "А70-1/2026", "pages": [{
        "page": 1, "text": "Синтетический текст для проверки хранения, не судебный акт."
    }]}]
    evidence, _ = normalize_evidence([{
        "case_id": "a", "page": 1, "quote": "Синтетический текст для проверки хранения",
    }], acts)
    data = {"cases": acts, "evidence": evidence, "comparisons": [{"case_id": "a"}],
            "report": {"summary": "Тест хранения"}}
    materials = Materials(store)
    materials.save(rid, data)
    materials.save(rid, data)
    assert materials.snapshot(rid)["evidence"] == evidence
    assert len(materials.snapshot(rid)["comparisons"]) == 1
    materials.save(rid, {**data, "comparisons": [], "evidence": []})
    assert materials.snapshot(rid)["evidence"] == []
    assert materials.snapshot(rid)["comparisons"] == []
    assert store.get(rid)["payload"]["title"] == "Синтетическая проверка"


def test_additive_materials_migration_and_idempotent_snapshot(tmp_path):
    exercise_materials(Store(tmp_path / "db"))
