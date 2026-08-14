"""V3-011 — migration 0047 is additive, reversible, and matches the models.

`tests/conftest.py` builds the test schema with `create_all`, not Alembic, so
nothing else in the suite would notice a model/migration divergence: the tests
would pass against a schema production never gets. This module compares the two
directly, without needing a database (the DB round-trip itself is the
`Alembic round-trip` CI job, which runs upgrade → downgrade -1 → upgrade).

The one thing to be careful with here is the `avatar_profiles ↔ avatar_versions`
cycle. That FK is not part of either `CREATE TABLE`; it is added by ALTER
afterwards and must be dropped *before* the tables on the way down. The
recorder below therefore has to understand `create_foreign_key` /
`drop_constraint` — a recorder that silently ignored them would assert a schema
this migration does not write.
"""

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa

from app.infrastructure.persistence.models.avatar import (
    AvatarProfile,
    AvatarSourceAsset,
    AvatarVersion,
)

MIGRATION_PATH = Path("alembic/versions/0047_add_avatar_persistence.py")
ACTIVE_VERSION_FK = "fk_avatar_profiles_active_version_id_avatar_versions"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0047_add_avatar_persistence", MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _OpRecorder:
    def __init__(self) -> None:
        self.created_tables: dict[str, tuple[Any, ...]] = {}
        self.created_indexes: list[tuple[str, str, list[str], bool]] = []
        self.created_foreign_keys: list[tuple[str, str, str, list[str], list[str], str | None]] = []
        self.dropped_constraints: list[tuple[str, str]] = []
        self.dropped_indexes: list[tuple[str, str]] = []
        self.dropped_tables: list[str] = []
        #: Every mutating call in order, so downgrade ordering can be asserted
        #: rather than inferred from three separate lists.
        self.calls: list[tuple[str, str]] = []

    def f(self, name: str) -> str:
        return name

    def create_table(self, name: str, *elements: Any) -> None:
        self.created_tables[name] = elements
        self.calls.append(("create_table", name))

    def create_index(self, name: str, table_name: str, columns: list[str], unique: bool = False) -> None:
        self.created_indexes.append((name, table_name, columns, unique))
        self.calls.append(("create_index", name))

    def create_foreign_key(
        self,
        constraint_name: str,
        source_table: str,
        referent_table: str,
        local_cols: list[str],
        remote_cols: list[str],
        ondelete: str | None = None,
    ) -> None:
        self.created_foreign_keys.append(
            (constraint_name, source_table, referent_table, local_cols, remote_cols, ondelete)
        )
        self.calls.append(("create_foreign_key", constraint_name))

    def drop_constraint(self, constraint_name: str, table_name: str, type_: str | None = None) -> None:
        self.dropped_constraints.append((constraint_name, table_name))
        self.calls.append(("drop_constraint", constraint_name))

    def drop_index(self, name: str, table_name: str) -> None:
        self.dropped_indexes.append((name, table_name))
        self.calls.append(("drop_index", name))

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)
        self.calls.append(("drop_table", name))


def _run_upgrade(monkeypatch: Any) -> _OpRecorder:
    migration = _load_migration()
    recorder = _OpRecorder()
    monkeypatch.setattr(migration, "op", recorder)
    migration.upgrade()
    return recorder


def _columns(recorder: _OpRecorder, table: str) -> set[str]:
    return {element.name for element in recorder.created_tables[table] if isinstance(element, sa.Column)}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _checks(elements: tuple[Any, ...]) -> set[str]:
    return {_normalize(str(element.sqltext)) for element in elements if isinstance(element, sa.CheckConstraint)}


def _model_checks(table: sa.Table) -> set[str]:
    return {
        _normalize(str(constraint.sqltext))
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }


def _unique_columns(constraint: sa.UniqueConstraint) -> tuple[str, ...]:
    """Column names of a UniqueConstraint, attached to a Table or not.

    The migration builds its constraints standalone inside `create_table(...)`,
    where `.columns` is still empty and the arguments sit unresolved — so
    comparing `.columns` alone would compare `()` against `()` for every
    constraint and pass no matter what the migration says.
    """
    names = tuple(column.name for column in constraint.columns)
    if names:
        return names
    return tuple(
        argument if isinstance(argument, str) else argument.name
        for argument in getattr(constraint, "_pending_colargs", ())
    )


def _uniques(elements: tuple[Any, ...]) -> set[tuple[str | None, tuple[str, ...]]]:
    return {
        (element.name, _unique_columns(element)) for element in elements if isinstance(element, sa.UniqueConstraint)
    }


def _model_uniques(table: sa.Table) -> set[tuple[str | None, tuple[str, ...]]]:
    return {
        (constraint.name, _unique_columns(constraint))
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }


TABLES = {
    "avatar_source_assets": AvatarSourceAsset,
    "avatar_profiles": AvatarProfile,
    "avatar_versions": AvatarVersion,
}


def test_revision_chains_onto_the_current_head() -> None:
    migration = _load_migration()
    assert migration.revision == "0047_add_avatar_persistence"
    assert migration.down_revision == "0046_add_purchase_decision_persistence"


def test_upgrade_creates_all_three_tables_matching_the_models(monkeypatch: Any) -> None:
    recorder = _run_upgrade(monkeypatch)

    assert set(recorder.created_tables) == set(TABLES)
    for table_name, model in TABLES.items():
        assert _columns(recorder, table_name) == {column.name for column in model.__table__.columns}
    # A version is written once and then only retired — no updated_at.
    assert "updated_at" not in _columns(recorder, "avatar_versions")


def test_check_constraints_match_the_models(monkeypatch: Any) -> None:
    recorder = _run_upgrade(monkeypatch)

    for table_name, model in TABLES.items():
        assert _checks(recorder.created_tables[table_name]) == _model_checks(model.__table__)


def test_unique_constraints_match_the_models(monkeypatch: Any) -> None:
    recorder = _run_upgrade(monkeypatch)

    for table_name, model in TABLES.items():
        assert _uniques(recorder.created_tables[table_name]) == _model_uniques(model.__table__)
    # Prompt-once and one-profile-per-user are the two structural rules; if
    # either constraint were dropped the feature would still "work" and quietly
    # lose its guarantee, so they are named explicitly.
    assert ("one_row_per_source_object", ("user_id", "source_object_key")) in _uniques(
        recorder.created_tables["avatar_source_assets"]
    )
    assert ("one_profile_per_user", ("user_id",)) in _uniques(recorder.created_tables["avatar_profiles"])


def test_indexes_match_the_models(monkeypatch: Any) -> None:
    recorder = _run_upgrade(monkeypatch)

    migrated = {(name, table, tuple(columns), unique) for name, table, columns, unique in recorder.created_indexes}
    modelled = {
        (index.name, table.name, tuple(c.name for c in index.columns), bool(index.unique))
        for model in TABLES.values()
        for table in (model.__table__,)
        for index in table.indexes
    }
    assert migrated == modelled


def test_the_cycle_closing_fk_is_added_by_alter_not_by_create_table(monkeypatch: Any) -> None:
    """`avatar_profiles.active_version_id` must not be inline in CREATE TABLE.

    Postgres cannot create `avatar_profiles` with an FK onto a table that does
    not exist yet, and `create_all` cannot order a cycle either — which is why
    the model declares it `use_alter=True`. If someone "simplified" this into
    the create_table call, table creation would fail for the whole suite.
    """
    recorder = _run_upgrade(monkeypatch)

    inline_fks = {
        tuple(element.column_keys)
        for element in recorder.created_tables["avatar_profiles"]
        if isinstance(element, sa.ForeignKeyConstraint)
    }
    assert ("active_version_id",) not in inline_fks

    assert recorder.created_foreign_keys == [
        (
            ACTIVE_VERSION_FK,
            "avatar_profiles",
            "avatar_versions",
            ["active_version_id"],
            ["id"],
            "SET NULL",
        )
    ]
    # SET NULL, not CASCADE: deleting a version must never delete the profile.
    # Paired with the active_version_matches_status CHECK this makes deleting
    # the version a `ready` profile points at fail outright.
    model_fk = next(
        constraint
        for constraint in AvatarProfile.__table__.constraints
        if isinstance(constraint, sa.ForeignKeyConstraint)
        and [column.name for column in constraint.columns] == ["active_version_id"]
    )
    assert model_fk.ondelete == "SET NULL"
    assert model_fk.use_alter is True


def test_upgrade_is_additive_only(monkeypatch: Any) -> None:
    """Acceptance 4: no existing table or column may be touched."""
    recorder = _run_upgrade(monkeypatch)

    assert recorder.dropped_tables == []
    assert recorder.dropped_indexes == []
    assert recorder.dropped_constraints == []
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    for destructive in ("drop_column", "alter_column", "rename_table"):
        assert destructive not in source


def test_downgrade_reverses_the_upgrade(monkeypatch: Any) -> None:
    migration = _load_migration()
    recorder = _OpRecorder()
    monkeypatch.setattr(migration, "op", recorder)
    migration.upgrade()
    upgrade_calls = len(recorder.calls)
    migration.downgrade()

    assert recorder.dropped_tables == ["avatar_versions", "avatar_profiles", "avatar_source_assets"]
    assert {name for name, _table, _columns, _unique in recorder.created_indexes} == {
        name for name, _table in recorder.dropped_indexes
    }
    assert recorder.dropped_constraints == [(ACTIVE_VERSION_FK, "avatar_profiles")]

    # The FK must go first, before any table drop — otherwise the round-trip
    # fails on a dependency that cannot be resolved in either direction.
    downgrade_calls = recorder.calls[upgrade_calls:]
    assert downgrade_calls[0] == ("drop_constraint", ACTIVE_VERSION_FK)
