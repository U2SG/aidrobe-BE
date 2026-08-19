"""V3-024 — migration 0051 is additive, reversible, and matches the model.

`tests/conftest.py` builds the test schema with `create_all`, not Alembic, so
nothing else in the suite would notice a model/migration divergence — the suite
would pass against a schema production never gets. That gap is not theoretical
here: `RecordPreviewFeedback` upserts through `ON CONFLICT (asset_id)`, which
needs a real unique index, so an index declared in only one of the two places
either breaks every request in production or every test in CI.

Structure copied from `test_v3_decision_migration.py`; no database is needed
(the round-trip itself is CI's `Alembic round-trip` job, upgrade → downgrade -1
→ upgrade).
"""

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa

from app.common.enums import PreviewFeedbackVerdict
from app.infrastructure.persistence.models.preview_feedback import StaticPreviewFeedback

MIGRATION_PATH = Path("alembic/versions/0051_add_static_preview_feedback.py")
TABLE = "static_preview_feedback"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0051_add_static_preview_feedback", MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _OpRecorder:
    def __init__(self) -> None:
        self.created_tables: dict[str, tuple[Any, ...]] = {}
        self.created_indexes: list[tuple[str, str, list[str], bool]] = []
        self.dropped_indexes: list[tuple[str, str]] = []
        self.dropped_tables: list[str] = []

    def f(self, name: str) -> str:
        return name

    def create_table(self, name: str, *elements: Any) -> None:
        self.created_tables[name] = elements

    def create_index(self, name: str, table_name: str, columns: list[str], unique: bool = False) -> None:
        self.created_indexes.append((name, table_name, columns, unique))

    def drop_index(self, name: str, table_name: str) -> None:
        self.dropped_indexes.append((name, table_name))

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)


def _run_upgrade(monkeypatch: Any) -> _OpRecorder:
    migration = _load_migration()
    recorder = _OpRecorder()
    monkeypatch.setattr(migration, "op", recorder)
    migration.upgrade()
    return recorder


def _normalize(sqltext: Any) -> str:
    return re.sub(r"\s+", " ", str(sqltext)).strip()


def test_revision_chains_onto_the_current_head() -> None:
    migration = _load_migration()
    assert migration.revision == "0051_add_static_preview_feedback"
    assert migration.down_revision == "0050_add_visual_asset_quality_signals"


def test_upgrade_creates_the_table_matching_the_model(monkeypatch: Any) -> None:
    recorder = _run_upgrade(monkeypatch)

    assert set(recorder.created_tables) == {TABLE}
    migrated = {
        element.name
        for element in recorder.created_tables[TABLE]
        if isinstance(element, sa.Column)
    }
    assert migrated == {column.name for column in StaticPreviewFeedback.__table__.columns}


def test_check_constraints_match_the_model(monkeypatch: Any) -> None:
    recorder = _run_upgrade(monkeypatch)

    migrated = {
        _normalize(element.sqltext)
        for element in recorder.created_tables[TABLE]
        if isinstance(element, sa.CheckConstraint)
    }
    modelled = {
        _normalize(constraint.sqltext)
        for constraint in StaticPreviewFeedback.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert migrated == modelled
    # And the literals really are the frozen vocabulary, not a stale copy of it.
    verdicts = {member.value for member in PreviewFeedbackVerdict}
    check = next(iter(migrated))
    assert all(f"'{value}'" in check for value in verdicts)


def test_indexes_match_the_model(monkeypatch: Any) -> None:
    recorder = _run_upgrade(monkeypatch)

    migrated = {(name, table, tuple(columns), unique) for name, table, columns, unique in recorder.created_indexes}
    modelled = {
        (index.name, TABLE, tuple(column.name for column in index.columns), bool(index.unique))
        for index in StaticPreviewFeedback.__table__.indexes
    }
    assert migrated == modelled
    # The ON CONFLICT target, and the reason a repeated tap cannot double-count.
    assert ("uq_static_preview_feedback_asset_id", TABLE, ("asset_id",), True) in migrated


def test_both_foreign_keys_cascade(monkeypatch: Any) -> None:
    """Acceptance 5 is carried by the database, not by application code — see
    `test_v3_preview_feedback_lifecycle.py` for the behavioral half."""
    recorder = _run_upgrade(monkeypatch)

    cascades = {
        tuple(constraint.column_keys): constraint.ondelete
        for constraint in recorder.created_tables[TABLE]
        if isinstance(constraint, sa.ForeignKeyConstraint)
    }
    assert cascades == {("user_id",): "CASCADE", ("asset_id",): "CASCADE"}


def test_upgrade_is_additive_only(monkeypatch: Any) -> None:
    """No human gate is required precisely because nothing existing is touched."""
    recorder = _run_upgrade(monkeypatch)

    assert recorder.dropped_tables == []
    assert recorder.dropped_indexes == []
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    for destructive in ("drop_column", "alter_column", "rename_table", "add_column"):
        assert destructive not in source


def test_downgrade_reverses_the_upgrade(monkeypatch: Any) -> None:
    migration = _load_migration()
    recorder = _OpRecorder()
    monkeypatch.setattr(migration, "op", recorder)
    migration.upgrade()
    migration.downgrade()

    assert recorder.dropped_tables == [TABLE]
    assert {name for name, _table, _columns, _unique in recorder.created_indexes} == {
        name for name, _table in recorder.dropped_indexes
    }
