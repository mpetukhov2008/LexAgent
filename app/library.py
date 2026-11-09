"""Small persistent act library; lexical retrieval is an explicit baseline."""

import hashlib
import json
import re
import time

from pydantic import Field, field_validator

from app.schemas import StrictModel
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

    def save(self, case):
        text = "\n".join(page["text"] for page in case["pages"])
        with self.store.connect() as db:
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

    def search(self, query):
        terms = list(dict.fromkeys(re.findall(r"[\w-]{3,}", query.casefold())))[:12]
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT payload,text_content FROM judicial_acts ORDER BY "
                "imported_at DESC LIMIT 1000"
            ).fetchall()
        scored = [
            (sum(term[:7] in row["text_content"].casefold() for term in terms), row) for row in rows
        ]
        result, seen = [], set()
        for score, row in sorted(scored, key=lambda item: item[0], reverse=True):
            case = json.loads(row["payload"])
            if score and case["number"] not in seen:
                result.append(case)
                seen.add(case["number"])
        return result[:10]
