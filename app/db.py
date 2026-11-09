import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Conflict(Exception):
    pass


class LostLease(Exception):
    pass


class Store:
    """Short transactions; persistent queue with fenced leases and atomic checkpoints."""

    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError("Unsupported database schema")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, parent_id TEXT REFERENCES runs(id),
                    idempotency_key TEXT UNIQUE NOT NULL, input_hash TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL,
                    checkpoint TEXT NOT NULL DEFAULT '{}', result TEXT,
                    error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    owner TEXT, lease_until REAL, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    created_at REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
                CREATE INDEX IF NOT EXISTS queue_status ON runs(status, created_at);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def event(self, db, run_id, kind, detail):
        db.execute(
            "INSERT INTO events(run_id,created_at,kind,detail) VALUES(?,?,?,?)",
            (run_id, time.time(), kind, dump(detail)),
        )

    def create(self, payload, key, parent_id=None):
        encoded = dump(payload)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id,input_hash,parent_id FROM runs WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row:
                if row["input_hash"] != digest or row["parent_id"] != parent_id:
                    raise Conflict("Этот ключ запроса уже использован для других данных")
                return row["id"], False
            run_id = str(uuid.uuid4())
            now = time.time()
            db.execute(
                "INSERT INTO runs(id,parent_id,idempotency_key,input_hash,payload,status,"
                "created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?)",
                (run_id, parent_id, key, digest, encoded, now, now),
            )
            self.event(db, run_id, "queued", {"message": "Задание сохранено в очереди"})
            return run_id, True

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        for key in ("payload", "checkpoint", "result"):
            value[key] = json.loads(value[key]) if value[key] else None
        return value

    def recent(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,status,payload,created_at,parent_id FROM runs "
                "ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
        return [
            dict(
                id=r["id"],
                status=r["status"],
                title=json.loads(r["payload"])["title"],
                mode=json.loads(r["payload"])["mode"],
                created_at=r["created_at"],
                parent_id=r["parent_id"],
            )
            for r in rows
        ]

    def events(self, run_id):
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,created_at,kind,detail FROM events WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    def claim(self, owner, lease_seconds, max_attempts):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            expired = db.execute(
                "SELECT id,attempts FROM runs WHERE status='running' AND lease_until<?", (now,)
            ).fetchall()
            for row in expired:
                failed = row["attempts"] >= max_attempts
                db.execute(
                    "UPDATE runs SET status=?,owner=NULL,lease_until=NULL,error=?,updated_at=? "
                    "WHERE id=?",
                    (
                        "failed" if failed else "queued",
                        "Лимит восстановлений исчерпан" if failed else None,
                        now,
                        row["id"],
                    ),
                )
                self.event(
                    db,
                    row["id"],
                    "recovery",
                    {
                        "message": "Срок аренды истек; контрольная точка сохранена",
                        "requeued": not failed,
                    },
                )
            row = db.execute(
                "SELECT id FROM runs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE runs SET status='running',owner=?,lease_until=?,attempts=attempts+1,"
                "updated_at=? WHERE id=?",
                (owner, now + lease_seconds, now, row["id"]),
            )
            self.event(db, row["id"], "started", {"message": "Агент разбора ситуации запущен"})
        return self.get(row["id"])

    def heartbeat(self, run_id, owner, seconds):
        with self.connect() as db:
            changed = db.execute(
                "UPDATE runs SET lease_until=? WHERE id=? AND owner=? AND status='running'",
                (time.time() + seconds, run_id, owner),
            ).rowcount
            if not changed:
                raise LostLease()

    def checkpoint(self, run_id, owner, state, kind, detail):
        with self.connect() as db:
            changed = db.execute(
                "UPDATE runs SET checkpoint=?,updated_at=? WHERE id=? AND owner=? "
                "AND status='running'",
                (dump(state), time.time(), run_id, owner),
            ).rowcount
            if not changed:
                raise LostLease()
            self.event(db, run_id, kind, detail)

    def finish(self, run_id, owner, result=None, error=None):
        status = (
            "failed" if error else ("needs_clarification" if result["questions"] else "completed")
        )
        with self.connect() as db:
            changed = db.execute(
                "UPDATE runs SET status=?,result=?,error=?,owner=NULL,lease_until=NULL,"
                "updated_at=? WHERE id=? AND owner=? AND status='running'",
                (status, dump(result) if result else None, error, time.time(), run_id, owner),
            ).rowcount
            if not changed:
                raise LostLease()
            self.event(db, run_id, status, {"message": error or "Карточка ситуации сохранена"})

    def release(self, run_id, owner):
        with self.connect() as db:
            changed = db.execute(
                "UPDATE runs SET status='queued',owner=NULL,lease_until=NULL WHERE id=? "
                "AND owner=? AND status='running'",
                (run_id, owner),
            ).rowcount
            if changed:
                self.event(
                    db, run_id, "recovery", {"message": "Остановка; продолжим с контрольной точки"}
                )

    def retry(self, run_id):
        with self.connect() as db:
            changed = db.execute(
                "UPDATE runs SET status='queued',checkpoint='{}',error=NULL,result=NULL,"
                "attempts=0,updated_at=? WHERE id=? AND status='failed'",
                (time.time(), run_id),
            ).rowcount
            if not changed:
                raise Conflict("Повтор доступен только для задания с ошибкой")
            self.event(db, run_id, "retry", {"message": "Повтор по запросу пользователя"})

    def cancel(self, run_id):
        with self.connect() as db:
            changed = db.execute(
                "UPDATE runs SET status='cancelled',owner=NULL,lease_until=NULL,updated_at=? "
                "WHERE id=? AND status IN ('queued','running')", (time.time(), run_id)
            ).rowcount
            if not changed:
                raise Conflict("Остановить можно только выполняющееся задание или очередь")
            self.event(db, run_id, "cancelled", {"message": "Остановлено пользователем; "
                       "выполняющийся сетевой запрос может завершиться, результат не перезапишется"})

    def resume(self, run_id):
        with self.connect() as db:
            changed = db.execute(
                "UPDATE runs SET status='queued',owner=NULL,lease_until=NULL,error=NULL,"
                "result=NULL,attempts=0,updated_at=? WHERE id=? AND status IN ('failed','cancelled')",
                (time.time(), run_id),
            ).rowcount
            if not changed:
                raise Conflict("Продолжить можно только остановленное задание или ошибку")
            self.event(db, run_id, "resumed", {"message": "Продолжение с контрольной точки"})
