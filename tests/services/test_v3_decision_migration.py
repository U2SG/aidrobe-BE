"""V3-003 — migration 0046 is additive, reversible, and matches the models.

`tests/conftest.py` builds the test schema with `create_all`, not Alembic, so
nothing else in the suite would notice a model/migration divergence: the tests
would pass against a schema production never gets. This module compares the two
directly, without needing a database (the DB round-trip itself is the
`Alembic round-trip` CI job, which runs upgrade → downgrade -1 → upgrade).
"""

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa

from app.infrastructure.persistence.models.purchase_decision import (
    PurchaseCandidate,
    WardrobeDecision,
)

MIGRATION_PATH = Path("alembic/versions/0046_add_purchase_decision_persistence.py")


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0046_add_purchase_decision_persistence", MIGRATION_PATH)
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


def _columns(recorder: _OpRecorder, table: str) -> set[str]:
    return {element.name for element in recorder.created_tables[table] if isinstance(element, sa.Column)}


def _checks(elements: tuple[Any, ...]) -> set[str]:
    return {
        re.sub(r"\s+", " ", str(element.sqltext)).strip()
        for element in elements
        if isinstance(element, sa.CheckConstraint)
    }


def _model_checks(table: sa.Table) -> set[str]:
    return {
        re.sub(r"\s+", " ", str(constraint.sqltext)).strip()
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }


def test_revision_chains_onto_the_current_head(monkeypatch: Any) -> None:
    migration = _load_migration()
    assert migration.revision == "0046_add_purchase_decision_persistence"
    assert migration.down_revision == "0045_add_recommendation_traces"


def test_upgrade_creates_both_tables_matching_the_models(monkeypatch: Any) -> None:
    recorder = _run_upgrade(monkeypatch)

    assert set(recorder.created_tables) == {"purchase_candidates", "wardrobe_decisions"}
    assert _columns(recorder, "purchase_candidates") == {column.name for column in PurchaseCandidate.__table__.columns}
    assert _columns(recorder, "wardrobe_decisions") == {column.name for column in WardrobeDecision.__table__.columns}
    # A decision row is written once and never revised — no updated_at anywhere.
    assert "updated_at" not in _columns(recorder, "wardrobe_decisions")


def test_check_constraints_match_the_models(monkeypatch: Any) -> None:
    recorder = _run_upgrade(monkeypatch)

    assert _checks(recorder.created_tables["purchase_candidates"]) == _model_checks(PurchaseCandidate.__table__)
    assert _checks(recorder.created_tables["wardrobe_decisions"]) == _model_checks(WardrobeDecision.__table__)


def test_indexes_match_the_models(monkeypatch: Any) -> None:
    recorder = _run_upgrade(monkeypatch)

    migrated = {(name, table, tuple(columns), unique) for name, table, columns, unique in recorder.created_indexes}
    modelled = {
        (index.name, table.name, tuple(c.name for c in index.columns), bool(index.unique))
        for table in (PurchaseCandidate.__table__, WardrobeDecision.__table__)
        for index in table.indexes
    }
    assert migrated == modelled
    # Idempotency of the decision itself rides on this one being unique.
    assert (
        "uq_wardrobe_decisions_candidate_id",
        "wardrobe_decisions",
        ("candidate_id",),
        True,
    ) in migrated


def test_upgrade_is_additive_only(monkeypatch: Any) -> None:
    """The V3-003 human gate: no existing table or column may be touched."""
    recorder = _run_upgrade(monkeypatch)

    assert recorder.dropped_tables == []
    assert recorder.dropped_indexes == []
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    for destructive in ("drop_column", "alter_column", "rename_table"):
        assert destructive not in source


def test_downgrade_reverses_the_upgrade(monkeypatch: Any) -> None:
    migration = _load_migration()
    recorder = _OpRecorder()
    monkeypatch.setattr(migration, "op", recorder)
    migration.upgrade()
    migration.downgrade()

    # Child table first: wardrobe_decisions has the FK onto purchase_candidates.
    assert recorder.dropped_tables == ["wardrobe_decisions", "purchase_candidates"]
    assert {name for name, _table, _columns, _unique in recorder.created_indexes} == {
        name for name, _table in recorder.dropped_indexes
    }
