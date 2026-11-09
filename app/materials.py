"""Additive relational storage for cases, acts, evidence and per-run conclusions."""

import json

from app.contracts import registry


class Materials:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS domain_migrations (version INTEGER PRIMARY KEY)")
            db.execute("INSERT INTO domain_migrations(version) VALUES(1) ON CONFLICT DO NOTHING")
            for statement in (
                "CREATE TABLE IF NOT EXISTS case_registry ("
                "id TEXT PRIMARY KEY, number TEXT NOT NULL, payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS act_registry ("
                "id TEXT PRIMARY KEY, case_key TEXT NOT NULL REFERENCES case_registry(id), "
                "payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS evidence_records ("
                "id TEXT PRIMARY KEY, act_id TEXT NOT NULL REFERENCES act_registry(id), "
                "page INTEGER NOT NULL, quote TEXT NOT NULL, payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS run_evidence ("
                "run_id TEXT NOT NULL REFERENCES runs(id), "
                "evidence_id TEXT NOT NULL REFERENCES evidence_records(id), "
                "PRIMARY KEY(run_id,evidence_id))",
                "CREATE TABLE IF NOT EXISTS comparison_records ("
                "run_id TEXT NOT NULL REFERENCES runs(id), "
                "act_id TEXT NOT NULL REFERENCES act_registry(id), payload TEXT NOT NULL, "
                "PRIMARY KEY(run_id,act_id))",
                "CREATE TABLE IF NOT EXISTS report_records ("
                "run_id TEXT PRIMARY KEY REFERENCES runs(id), payload TEXT NOT NULL)",
            ):
                db.execute(statement)

    def save(self, run_id, search):
        cases, acts = registry(search.get("cases", []))
        encode = lambda value: json.dumps(value, ensure_ascii=False)  # noqa: E731
        with self.store.connect() as db:
            db.execute("DELETE FROM run_evidence WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM comparison_records WHERE run_id=?", (run_id,))
            for case in cases:
                db.execute("INSERT INTO case_registry(id,number,payload) VALUES(?,?,?) "
                           "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                           (case["id"], case["number"], encode(case)))
            for act in acts:
                db.execute("INSERT INTO act_registry(id,case_key,payload) VALUES(?,?,?) "
                           "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                           (act["id"], act["case_key"], encode(act)))
            for evidence in search.get("evidence", []):
                db.execute("INSERT INTO evidence_records(id,act_id,page,quote,payload) "
                           "VALUES(?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                           (evidence["id"], evidence["act_id"], evidence["page"],
                            evidence["quote"], encode(evidence)))
                db.execute("INSERT INTO run_evidence(run_id,evidence_id) VALUES(?,?) "
                           "ON CONFLICT DO NOTHING", (run_id, evidence["id"]))
            for comparison in search.get("comparisons", []):
                db.execute("INSERT INTO comparison_records(run_id,act_id,payload) VALUES(?,?,?) "
                           "ON CONFLICT(run_id,act_id) DO UPDATE SET payload=excluded.payload",
                           (run_id, comparison["case_id"], encode(comparison)))
            db.execute("INSERT INTO report_records(run_id,payload) VALUES(?,?) "
                       "ON CONFLICT(run_id) DO UPDATE SET payload=excluded.payload",
                       (run_id, encode(search.get("report", {}))))

    def snapshot(self, run_id):
        with self.store.connect() as db:
            evidence = db.execute(
                "SELECT e.payload FROM evidence_records e JOIN run_evidence r "
                "ON r.evidence_id=e.id WHERE r.run_id=? ORDER BY e.id", (run_id,)
            ).fetchall()
            comparisons = db.execute(
                "SELECT payload FROM comparison_records WHERE run_id=? ORDER BY act_id", (run_id,)
            ).fetchall()
            report = db.execute("SELECT payload FROM report_records WHERE run_id=?", (run_id,))
            report = report.fetchone()
        return {"contract_version": "2.0", "evidence": [json.loads(e["payload"]) for e in evidence],
                "comparisons": [json.loads(c["payload"]) for c in comparisons],
                "report": json.loads(report["payload"]) if report else None}
