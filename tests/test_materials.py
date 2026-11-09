from app.contracts import normalize_evidence
from app.db import Store
from app.materials import Materials


def exercise_materials(store):
    rid, _ = store.create({"title": "Синтетическая проверка", "mode": "demo"}, "materials-test")
    acts = [{"id": "a", "number": "А70-1/2026", "pages": [{
        "page": 1, "text": "Синтетический текст для проверки хранения, не судебный акт."
    }]}]
    acts[0].update(document_sha256="a" * 64, original_retained=True, text_origin="ocr",
                   page_quality=[{"page": 1, "origin": "ocr", "requires_review": True}])
    evidence, _ = normalize_evidence([{
        "case_id": "a", "page": 1, "quote": "Синтетический текст для проверки хранения",
    }], acts)
    data = {"cases": acts, "evidence": evidence, "comparisons": [{"case_id": "a"}],
            "report": {"summary": "Тест хранения"}}
    materials = Materials(store)
    materials.save(rid, data)
    materials.save(rid, data)
    with store.connect() as db:
        assert db.execute("SELECT sha256 FROM act_documents WHERE act_id=?", ("a",)).fetchone()[
            "sha256"] == "a" * 64
        assert db.execute("SELECT count(*) AS n FROM original_documents").fetchone()["n"] == 1
        payload = db.execute("SELECT payload FROM act_registry WHERE id=?", ("a",)).fetchone()
        assert '"text_origin": "ocr"' in payload["payload"]
    assert materials.snapshot(rid)["evidence"] == evidence
    assert len(materials.snapshot(rid)["comparisons"]) == 1
    materials.save(rid, {**data, "comparisons": [], "evidence": []})
    assert materials.snapshot(rid)["evidence"] == []
    assert materials.snapshot(rid)["comparisons"] == []
    assert store.get(rid)["payload"]["title"] == "Синтетическая проверка"


def test_additive_materials_migration_and_idempotent_snapshot(tmp_path):
    exercise_materials(Store(tmp_path / "db"))
