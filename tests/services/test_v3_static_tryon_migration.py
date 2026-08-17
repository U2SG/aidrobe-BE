"""V3-016 — migration 0048 is additive, reversible, and matches the model.

`tests/conftest.py` builds the test schema with `create_all`, not Alembic, so
nothing else in the suite would notice a model/migration divergence: every test
would pass against a schema production never gets. This module compares the two
directly and needs no database — the DB round-trip itself is the
`Alembic round-trip` CI job (`.github/workflows/ci.yml`), which runs
upgrade → downgrade -1 → upgrade against pgvector/pg16.

Same instrument as `test_v3_avatar_migration.py`, reduced to the operations 0048
actually performs. The one thing worth being careful with here is **ordering on
the way down**: the CHECK references all three new columns, so dropping a column
before dropping the constraint fails. A recorder that only collected sets could
not see that, so the calls are recorded in order.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa

from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset

MIGRATION_PATH = Path("alembic/versions/0048_add_visual_asset_provenance.py")
TABLE = "outfit_visual_assets"
CONSTRAINT = "ck_outfit_visual_assets_tryon_records_provenance"
NEW_COLUMNS = ("provider", "model_version", "prompt_version")


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0048", MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _OpRecorder:
    def __init__(self) -> None:
        self.added_columns: list[tuple[str, sa.Column[Any]]] = []
        self.dropped_columns: list[tuple[str, str]] = []
        self.created_checks: list[tuple[str, str, str]] = []
        self.dropped_constraints: list[tuple[str, str]] = []
        #: Every mutating call in order, so downgrade ordering is asserted rather
        #: than inferred from separate lists.
        self.calls: list[tuple[str, str]] = []

    def f(self, name: str) -> str:
        return name

    def add_column(self, table_name: str, column: sa.Column[Any]) -> None:
        self.added_columns.append((table_name, column))
        self.calls.append(("add_column", column.name))

    def drop_column(self, table_name: str, column_name: str) -> None:
        self.dropped_columns.append((table_name, column_name))
        self.calls.append(("drop_column", column_name))

    def create_check_constraint(self, name: str, table_name: str, condition: str) -> None:
        self.created_checks.append((name, table_name, condition))
        self.calls.append(("create_check_constraint", name))

    def drop_constraint(self, name: str, table_name: str, type_: str | None = None) -> None:
        self.dropped_constraints.append((name, table_name))
        self.calls.append(("drop_constraint", name))


def _run(direction: str, monkeypatch: Any) -> _OpRecorder:
    migration = _load_migration()
    recorder = _OpRecorder()
    monkeypatch.setattr(migration, "op", recorder)
    getattr(migration, direction)()
    return recorder


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def test_the_migration_adds_exactly_the_three_provenance_columns(monkeypatch: Any) -> None:
    recorder = _run("upgrade", monkeypatch)

    assert [table for table, _ in recorder.added_columns] == [TABLE] * 3
    assert tuple(column.name for _, column in recorder.added_columns) == NEW_COLUMNS
    for _, column in recorder.added_columns:
        assert column.nullable is True, f"{column.name} must be nullable — M10 rows predate it"
        assert isinstance(column.type, sa.String)
        assert column.type.length == 64


def test_the_migration_creates_the_same_check_the_model_declares(monkeypatch: Any) -> None:
    """The two must not drift: `create_all` uses the model, production uses this.

    Compared as normalized SQL text rather than by name alone, because a
    constraint with the right name and a weaker predicate is exactly the failure
    this test exists to catch.
    """
    recorder = _run("upgrade", monkeypatch)

    assert len(recorder.created_checks) == 1
    name, table, condition = recorder.created_checks[0]
    assert (name, table) == (CONSTRAINT, TABLE)

    model_checks = {
        _normalize(str(constraint.sqltext))
        for constraint in OutfitVisualAsset.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert _normalize(condition) in model_checks


def test_the_model_carries_the_check_under_the_conventional_name() -> None:
    """`ck_%(table_name)s_%(constraint_name)s` — so the two names agree."""
    names = {
        constraint.name
        for constraint in OutfitVisualAsset.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert CONSTRAINT in names


def test_the_model_declares_the_three_columns_as_nullable_strings() -> None:
    columns = OutfitVisualAsset.__table__.c
    for name in NEW_COLUMNS:
        assert name in columns, f"{name} missing from the model"
        assert columns[name].nullable is True


def test_the_downgrade_reverses_the_upgrade_and_drops_the_check_first(
    monkeypatch: Any,
) -> None:
    """Additive and reversible (the human gate on every V3 migration).

    Ordering is asserted, not assumed: the CHECK references all three columns, so
    a downgrade that dropped a column first would fail against a real database —
    and this recorder-based test is the only place that failure is visible
    without one.
    """
    recorder = _run("downgrade", monkeypatch)

    assert recorder.dropped_constraints == [(CONSTRAINT, TABLE)]
    assert [column for _, column in recorder.dropped_columns] == list(reversed(NEW_COLUMNS))
    assert recorder.calls[0][0] == "drop_constraint"
    # Nothing is created on the way down, and nothing else is touched.
    assert {call for call, _ in recorder.calls} == {"drop_constraint", "drop_column"}


def test_the_migration_is_purely_additive(monkeypatch: Any) -> None:
    """No table, no rename, no backfill, no data statement in `upgrade`.

    Read from the source rather than from the recorder, because an `op.execute`
    the recorder does not implement would raise rather than being reported — and
    a reader of this test should be able to see that destructive operations are
    absent by name.
    """
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    upgrade_body = source.split("def upgrade()")[1].split("def downgrade()")[0]

    for forbidden in ("drop_table", "drop_column", "alter_column", "execute", "rename"):
        assert forbidden not in upgrade_body, f"upgrade performs {forbidden}"
