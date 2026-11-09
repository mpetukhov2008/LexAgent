"""Small persistent act library; lexical retrieval is an explicit baseline."""

import hashlib
import json
import time

from pydantic import Field, field_validator

from app.schemas import StrictModel
from app.search_agent import terms
from app.sources import court_url


class ActImport(StrictModel):
    number: str = Field(min_length=3, max_length=100)
    court: str = Field(min_length=3, max_length=200)
    date: str = Field(default="", max_length=30)
    url: str = Field(max_length=1000)
    text: str = Field(min_length=50, max_length=100000)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        if not court_url(value):
            raise ValueError("Нужна HTTPS-ссылка kad.arbitr.ru или ras.arbitr.ru")
        return value


class Library:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS judicial_acts (
                id TEXT PRIMARY KEY, number TEXT NOT NULL, payload TEXT NOT NULL,
                text_content TEXT NOT NULL, imported_at DOUBLE PRECISION NOT NULL
            )""")
            db.execute("CREATE TABLE IF NOT EXISTS document_identity ("
                       "sha256 TEXT PRIMARY KEY, act_id TEXT NOT NULL, number TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS document_aliases ("
                       "source_act_id TEXT PRIMARY KEY, canonical_act_id TEXT NOT NULL)")
            if hasattr(store, "url"):
                db.execute("CREATE INDEX IF NOT EXISTS judicial_acts_fts ON judicial_acts "
                           "USING gin(to_tsvector('russian',text_content))")

    def save(self, case):
        text = "\n".join(page["text"] for page in case["pages"])
        with self.store.connect() as db:
            digest = case.get("document_sha256")
            if digest:
                original_id = case.get("source_act_id", case["id"])
                content_id = "pdf_" + digest[:24]
                db.execute("INSERT INTO document_identity(sha256,act_id,number) VALUES(?,?,?) "
                           "ON CONFLICT(sha256) DO NOTHING", (digest, content_id, case["number"]))
                canonical = db.execute("SELECT act_id,number FROM document_identity WHERE sha256=?",
                                       (digest,)).fetchone()
                if canonical["number"].upper().replace("A", "А") != (
                        case["number"].upper().replace("A", "А")):
                    from app.agent import AgentError

                    raise AgentError("Один PDF связан с разными номерами дел; нужна проверка")
                case["source_act_id"] = original_id
                case["id"] = content_id
                if canonical["act_id"] != content_id:
                    db.execute("UPDATE document_identity SET act_id=? WHERE sha256=?",
                               (content_id, digest))
                    db.execute("UPDATE document_aliases SET canonical_act_id=? "
                               "WHERE canonical_act_id=?", (content_id, canonical["act_id"]))
                old = db.execute("SELECT payload FROM judicial_acts WHERE id=?",
                                 (original_id,)).fetchone()
                if old and original_id != content_id and json.loads(old["payload"]).get(
                        "document_sha256") == digest:
                    db.execute("DELETE FROM judicial_acts WHERE id=?", (original_id,))
                db.execute("INSERT INTO document_aliases(source_act_id,canonical_act_id) "
                           "VALUES(?,?) "
                           "ON CONFLICT(source_act_id) DO UPDATE SET "
                           "canonical_act_id=excluded.canonical_act_id", (original_id, case["id"]))
            db.execute(
                "INSERT INTO judicial_acts(id,number,payload,text_content,imported_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "payload=excluded.payload,text_content=excluded.text_content,imported_at=excluded.imported_at",
                (
                    case["id"],
                    case["number"],
                    json.dumps(case, ensure_ascii=False),
                    text,
                    time.time(),
                ),
            )

    def import_act(self, value):
        case = {
            "id": hashlib.sha256((value.url + value.text).encode()).hexdigest()[:16],
            "number": value.number,
            "court": value.court,
            "date": value.date,
            "kind": "Текст, загруженный пользователем",
            "case_url": value.url,
            "file_url": value.url,
            "appeal_status": "Не проверен",
            "quotes": [],
            "provenance": "user_import_unverified",
            "pages": [
                {"page": i + 1, "text": text} for i, text in enumerate(value.text.split("\f"))
            ],
        }
        self.save(case)
        return case["id"]

    def recent(self):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT id,number,imported_at FROM judicial_acts ORDER BY "
                "imported_at DESC LIMIT 100"
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, identity):
        with self.store.connect() as db:
            alias = db.execute("SELECT canonical_act_id FROM document_aliases "
                               "WHERE source_act_id=?",
                               (identity,)).fetchone()
            row = db.execute("SELECT payload FROM judicial_acts WHERE id=?",
                             (alias["canonical_act_id"] if alias else identity,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def search(self, query):
        tokens = terms(query)
        with self.store.connect() as db:
            if hasattr(self.store, "url"):
                rows = db.execute(
                    "SELECT payload,text_content FROM judicial_acts WHERE "
                    "to_tsvector('russian',text_content) @@ plainto_tsquery('russian',?) "
                    "ORDER BY ts_rank(to_tsvector('russian',text_content), "
                    "plainto_tsquery('russian',?)) DESC LIMIT 100", (query, query)
                ).fetchall()
                if not rows:
                    rows = db.execute(
                        "SELECT payload,text_content FROM judicial_acts "
                        "ORDER BY imported_at DESC LIMIT 1000"
                    ).fetchall()
            else:
                rows = db.execute(
                    "SELECT payload,text_content FROM judicial_acts ORDER BY "
                    "imported_at DESC LIMIT 1000"
                ).fetchall()
        scored = [
            (sum(term in row["text_content"].casefold() for term in tokens), row) for row in rows
        ]
        result = []
        for score, row in sorted(scored, key=lambda item: item[0], reverse=True):
            case = json.loads(row["payload"])
            if score:
                result.append(case)
        return result[:10]
