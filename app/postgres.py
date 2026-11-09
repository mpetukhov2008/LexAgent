"""PostgreSQL queue implementing the same contract as the local SQLite store.

Short create/claim transactions use an advisory lock. Model/network work is
always outside transactions. No application-wide lock is held during analysis.
"""

from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from app.db import Store


class Connection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        if sql == "BEGIN IMMEDIATE":
            return self.connection.execute("SELECT pg_advisory_xact_lock(74521019)")
        return self.connection.execute(sql.replace("?", "%s"), params)


class PostgresStore(Store):
    def __init__(self, url):
        self.url = url
        with psycopg.connect(url, connect_timeout=5) as db:
            db.execute("SELECT pg_advisory_xact_lock(74521018)")
            db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
            current = db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
            if current is not None and current > 1:
                raise RuntimeError("Unsupported PostgreSQL schema version")
            db.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, parent_id TEXT REFERENCES runs(id),
                    idempotency_key TEXT UNIQUE NOT NULL, input_hash TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL,
                    checkpoint TEXT NOT NULL DEFAULT '{}', result TEXT,
                    error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    owner TEXT, lease_until DOUBLE PRECISION,
                    created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    created_at DOUBLE PRECISION NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id)")
            db.execute("CREATE INDEX IF NOT EXISTS queue_status ON runs(status,created_at)")
            db.execute("INSERT INTO schema_migrations(version) VALUES(1) ON CONFLICT DO NOTHING")

    @contextmanager
    def connect(self):
        with psycopg.connect(self.url, row_factory=dict_row, connect_timeout=5) as db:
            yield Connection(db)
