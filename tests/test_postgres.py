"""Run the backend contract against isolated real PostgreSQL schemas.

Set TEST_DATABASE_URL explicitly; production tables are never reused or cleared.
"""

import os
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.config import Settings
from app.postgres import PostgresStore
from tests import test_backend as contract


@pytest.fixture
def pg_settings(tmp_path, monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = "test_prospects_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped_url = make_conninfo(url, options=f"-csearch_path={schema}")
    settings = Settings(database=tmp_path / "unused", database_url=scoped_url, worker_enabled=False)
    # Existing contract functions instantiate Store directly; bind their factory to PG.
    monkeypatch.setattr(contract, "Store", lambda path: PostgresStore(scoped_url))
    try:
        yield settings
    finally:
        assert schema.startswith("test_prospects_") and len(schema) == 47
        with psycopg.connect(url, autocommit=True) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.parametrize(
    "check",
    [
        contract.test_idempotency_and_concurrent_queue_claim,
        contract.test_recovery_fences_old_worker_and_preserves_checkpoint,
        contract.test_recovery_attempt_limit,
        contract.test_api_roundtrip_clarifications_and_export,
        contract.test_invalid_input_cross_origin_and_size,
        contract.test_yandex_requires_configuration_and_consent,
        contract.test_real_background_worker_completes,
    ],
)
def test_postgres_backend_contract(pg_settings, check):
    check(pg_settings)
