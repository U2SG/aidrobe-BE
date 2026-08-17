"""V3-018 — migration 0050 is additive, reversible, and matches the model.

Same instrument and same reason as `test_v3_static_preview_migration.py`:
`tests/conftest.py` builds the test schema with `create_all`, not Alembic, so a
model/migration divergence is invisible to every other test in the suite — they
would all pass against a schema production never gets. Needs no database; the
real round-trip is CI's `Alembic round-trip` job (upgrade → downgrade -1 →
upgrade against pgvector/pg16).

0050 is simpler than its two predecessors — one nullable JSONB column, no
index, no CHECK — so the interesting assertions are the negative ones: nothing
else moves, and there is deliberately NO `quality_status` column, because
`generation_status` already distinguishes ready / failed /
rejected_by_quality_gate and a second column restating it is the
derived-column smell.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset

MIGRATION_PATH = Path("alembic/versions/0050_add_visual_asset_quality_signals.py")
TABLE = "outfit_visual_assets"
COLUMN = "quality_signals"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0050", MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _OpRecorder:
    def __init__(self) -> None:
        self.added_columns: list[tuple[str, sa.Column[Any]]] = []
        self.dropped_columns: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []

    def add_column(self, table_name: str, column: sa.Column[Any]) -> None:
        self.added_columns.append((table_name, column))
        self.calls.append(("add_column", column.name))

    def drop_column(self, table_name: str, column_name: str) -> None:
        self.dropped_columns.append((table_name, column_name))
        self.calls.append(("drop_column", column_name))


def _run(direction: str, monkeypatch: Any) -> _OpRecorder:
    migration = _load_migration()
    recorder = _OpRecorder()
    monkeypatch.setattr(migration, "op", recorder)
    getattr(migration, direction)()
    return recorder


def test_the_migration_adds_exactly_one_nullable_jsonb_column(monkeypatch: Any) -> None:
    recorder = _run("upgrade", monkeypatch)

    assert [table for table, _ in recorder.added_columns] == [TABLE]
    (_, column) = recorder.added_columns[0]
    assert column.name == COLUMN
    # Nullable because every M10 row predates the gate, and a V3-017 row
    # written before this feature was judged by nothing — inventing a verdict
    # onto them would be worse than leaving them NULL (the 0048 reasoning).
    assert column.nullable is True
    assert isinstance(column.type, JSONB)


def test_the_model_declares_the_same_column() -> None:
    columns = OutfitVisualAsset.__table__.c
    assert COLUMN in columns
    assert columns[COLUMN].nullable is True
    assert isinstance(columns[COLUMN].type, JSONB)


def test_there_is_deliberately_no_quality_status_column() -> None:
    """V3_SPEC §11.3 *suggests* `quality_status`; the feature declines it.

    `generation_status` already carries ready / failed /
    rejected_by_quality_gate, so a second status column would be a derived
    column that can disagree with the truth. If someone later adds it, this
    test makes them say why the derivation argument stopped holding.
    """
    assert "quality_status" not in OutfitVisualAsset.__table__.c


def test_the_downgrade_reverses_the_upgrade_exactly(monkeypatch: Any) -> None:
    recorder = _run("downgrade", monkeypatch)

    assert recorder.dropped_columns == [(TABLE, COLUMN)]
    assert [call for call, _ in recorder.calls] == ["drop_column"]


def test_the_migration_is_purely_additive(monkeypatch: Any) -> None:
    """No table, no rename, no backfill, no data statement in `upgrade`."""
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    upgrade_body = source.split("def upgrade()")[1].split("def downgrade()")[0]

    for forbidden in ("drop_table", "drop_column", "alter_column", "execute", "rename"):
        assert forbidden not in upgrade_body, f"upgrade performs {forbidden}"


def test_the_revision_chain_continues_from_0049() -> None:
    """A mis-chained `down_revision` produces two Alembic heads, which the
    round-trip job reports as a confusing merge error rather than as "this
    migration is in the wrong place"."""
    migration = _load_migration()
    assert migration.revision == "0050_add_visual_asset_quality_signals"
    assert migration.down_revision == "0049_add_visual_asset_cache_key"
