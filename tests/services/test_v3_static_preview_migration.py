"""V3-017 — migration 0049 is additive, reversible, and matches the model.

Same instrument and same reason as `test_v3_static_tryon_migration.py`:
`tests/conftest.py` builds the test schema with `create_all`, not Alembic, so a
model/migration divergence is invisible to every other test in the suite — they
would all pass against a schema production never gets. Needs no database; the
real round-trip is CI's `Alembic round-trip` job (upgrade → downgrade -1 →
upgrade against pgvector/pg16).

The ordering hazard here is the mirror of 0048's: the unique index is defined
*over* the new column, so dropping the column before the index fails. A
set-based recorder could not see that, so calls are recorded in order.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa

from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset

MIGRATION_PATH = Path("alembic/versions/0049_add_visual_asset_cache_key.py")
TABLE = "outfit_visual_assets"
INDEX = "uq_outfit_visual_assets_cache_key"
COLUMN = "cache_key"
#: SHA-256 hex is 64 characters. The column is sized to it exactly.
DIGEST_CHARS = 64


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0049", MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _OpRecorder:
    def __init__(self) -> None:
        self.added_columns: list[tuple[str, sa.Column[Any]]] = []
        self.dropped_columns: list[tuple[str, str]] = []
        self.created_indexes: list[tuple[str, str, list[str], bool]] = []
        self.dropped_indexes: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []

    def f(self, name: str) -> str:
        return name

    def add_column(self, table_name: str, column: sa.Column[Any]) -> None:
        self.added_columns.append((table_name, column))
        self.calls.append(("add_column", column.name))

    def drop_column(self, table_name: str, column_name: str) -> None:
        self.dropped_columns.append((table_name, column_name))
        self.calls.append(("drop_column", column_name))

    def create_index(
        self, name: str, table_name: str, columns: list[str], unique: bool = False
    ) -> None:
        self.created_indexes.append((name, table_name, columns, unique))
        self.calls.append(("create_index", name))

    def drop_index(self, name: str, table_name: str | None = None) -> None:
        self.dropped_indexes.append((name, table_name or ""))
        self.calls.append(("drop_index", name))


def _run(direction: str, monkeypatch: Any) -> _OpRecorder:
    migration = _load_migration()
    recorder = _OpRecorder()
    monkeypatch.setattr(migration, "op", recorder)
    getattr(migration, direction)()
    return recorder


def test_the_migration_adds_exactly_one_nullable_digest_column(monkeypatch: Any) -> None:
    recorder = _run("upgrade", monkeypatch)

    assert [table for table, _ in recorder.added_columns] == [TABLE]
    (_, column) = recorder.added_columns[0]
    assert column.name == COLUMN
    # Nullable because every M10 row (`ootd_poster`, `outfit_board`) has no
    # cache key at all; a NOT NULL would need a value invented for each.
    assert column.nullable is True
    assert isinstance(column.type, sa.String)
    # Exact, not "at least": a shorter column would silently truncate two
    # distinct digests into one collision serving the wrong picture.
    assert column.type.length == DIGEST_CHARS


def test_the_index_is_unique_and_the_model_declares_the_same_one(
    monkeypatch: Any,
) -> None:
    """Uniqueness is the whole acceptance-1 guarantee, so it is asserted twice.

    Once on the migration (what production gets) and once on the model (what
    `create_all` gives the test suite). If they drifted, every services test
    would exercise a table where the duplicate-job race is simply allowed.
    """
    recorder = _run("upgrade", monkeypatch)

    assert len(recorder.created_indexes) == 1
    name, table, columns, unique = recorder.created_indexes[0]
    assert (name, table, columns) == (INDEX, TABLE, [COLUMN])
    assert unique is True

    model_indexes = {
        index.name: (index.unique, [c.name for c in index.columns])
        for index in OutfitVisualAsset.__table__.indexes
    }
    assert model_indexes.get(INDEX) == (True, [COLUMN])


def test_the_model_declares_the_column_as_a_nullable_64_char_string() -> None:
    columns = OutfitVisualAsset.__table__.c
    assert COLUMN in columns
    assert columns[COLUMN].nullable is True
    assert columns[COLUMN].type.length == DIGEST_CHARS


def test_the_downgrade_reverses_the_upgrade_and_drops_the_index_first(
    monkeypatch: Any,
) -> None:
    """Additive and reversible (the human gate on every V3 migration).

    Ordering asserted rather than assumed: the index is defined over the
    column, so dropping the column first fails against a real database — and
    this recorder is the only place that failure is visible without one.
    """
    recorder = _run("downgrade", monkeypatch)

    assert recorder.dropped_indexes == [(INDEX, TABLE)]
    assert recorder.dropped_columns == [(TABLE, COLUMN)]
    assert recorder.calls[0][0] == "drop_index"
    assert {call for call, _ in recorder.calls} == {"drop_index", "drop_column"}


def test_the_migration_is_purely_additive(monkeypatch: Any) -> None:
    """No table, no rename, no backfill, no data statement in `upgrade`."""
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    upgrade_body = source.split("def upgrade()")[1].split("def downgrade()")[0]

    for forbidden in ("drop_table", "drop_column", "alter_column", "execute", "rename"):
        assert forbidden not in upgrade_body, f"upgrade performs {forbidden}"


def test_the_revision_chain_continues_from_0048() -> None:
    """0048 is V3-016's provenance migration, which 0049's CHECK depends on.

    Asserted because a mis-chained `down_revision` produces two Alembic heads,
    which the round-trip job reports as a confusing merge error rather than as
    "this migration is in the wrong place".
    """
    migration = _load_migration()
    assert migration.revision == "0049_add_visual_asset_cache_key"
    assert migration.down_revision == "0048_add_visual_asset_provenance"
