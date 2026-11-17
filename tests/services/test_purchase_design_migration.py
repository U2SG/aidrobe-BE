"""Exercise 0058 on real legacy rows, including a rollback that must preserve history."""

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.infrastructure.persistence.models import Base
from app.infrastructure.persistence.models.purchase_decision import (
    PurchaseCandidate,
    WardrobeDecision,
    WardrobeDecisionChoice,
)


def _migration(filename: str) -> ModuleType:
    path = Path("alembic/versions") / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_upgrade_preserves_legacy_and_downgrade_refuses_to_delete_linked_history(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _migration("0046_add_purchase_decision_persistence.py")
    linked = _migration("0058_link_purchase_decisions.py")
    assert linked.down_revision == "0057_link_packing_to_designs"
    engine = create_async_engine(postgres_url)
    schema = "shopping_migration_" + uuid.uuid4().hex

    def exercise(connection: Connection) -> None:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
        connection.exec_driver_sql("CREATE TABLE users (id uuid PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE ask_sessions (id uuid PRIMARY KEY)")
        operations = Operations(MigrationContext.configure(connection, opts={"target_metadata": Base.metadata}))
        monkeypatch.setattr(old, "op", operations)
        monkeypatch.setattr(linked, "op", operations)
        old.upgrade()
        user_id, candidate_id, legacy_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        connection.execute(sa.text("INSERT INTO users (id) VALUES (:id)"), {"id": user_id})
        connection.execute(sa.text("""
            INSERT INTO purchase_candidates (id,user_id,source_type,source_url,status)
            VALUES (:id,:user_id,'share_link','https://example.com/coat','parsed')
        """), {"id": candidate_id, "user_id": user_id})
        base_insert = """
            INSERT INTO wardrobe_decisions
                (id,user_id,candidate_id,decision_type,snapshot_version,context_snapshot,evidence_snapshot,
                 decision,uncertainty,visual_need)
            VALUES (:id,:user_id,:candidate_id,'purchase',1,'{}','{}','wait','medium','none')
        """
        params = {"id": legacy_id, "user_id": user_id, "candidate_id": candidate_id}
        connection.execute(sa.text(base_insert), params)
        linked.upgrade()
        linked.downgrade()  # Existing legacy data is a supported rollback.
        linked.upgrade()
        assert connection.scalar(sa.text("SELECT id FROM wardrobe_decisions")) == legacy_id
        inspector = sa.inspect(connection)
        for model in (PurchaseCandidate, WardrobeDecision, WardrobeDecisionChoice):
            assert {column["name"] for column in inspector.get_columns(model.__tablename__)} == {
                column.name for column in model.__table__.columns
            }
        indexes = {index["name"]: index for index in inspector.get_indexes("wardrobe_decisions")}
        assert indexes["uq_wardrobe_decisions_legacy_candidate"]["unique"]
        assert "conversation_id IS NULL" in str(
            indexes["uq_wardrobe_decisions_legacy_candidate"]["dialect_options"]["postgresql_where"]
        )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(sa.text(base_insert), {**params, "id": uuid.uuid4()})
        linked_insert = """
            INSERT INTO wardrobe_decisions
                (id,user_id,candidate_id,decision_type,snapshot_version,context_snapshot,evidence_snapshot,
                 decision,uncertainty,visual_need,conversation_id,design_revision,request_id,request_fingerprint)
            VALUES (:id,:user_id,:candidate_id,'purchase',1,'{}','{}','wait','medium','none',
                    :conversation_id,:revision,:request_id,:fingerprint)
        """
        first = {**params, "id": uuid.uuid4(), "conversation_id": uuid.uuid4(), "revision": 1,
                 "request_id": uuid.uuid4(), "fingerprint": "a" * 64}
        connection.execute(sa.text(linked_insert), first)
        connection.execute(sa.text(linked_insert), {**first, "id": uuid.uuid4(), "revision": 2,
                                                   "request_id": uuid.uuid4()})
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(sa.text(linked_insert), {**first, "id": uuid.uuid4()})
        with pytest.raises(DBAPIError, match="multiple evaluations per candidate"), connection.begin_nested():
            linked.downgrade()
        assert connection.scalar(sa.text("SELECT count(*) FROM wardrobe_decisions")) == 3
        assert "wardrobe_decision_choices" in sa.inspect(connection).get_table_names()
        connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')

    try:
        async with engine.begin() as connection:
            await connection.run_sync(exercise)
    finally:
        await engine.dispose()
