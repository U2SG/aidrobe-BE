import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa


def _load_migration() -> ModuleType:
    path = Path("alembic/versions/0040_add_outfit_favorites.py")
    spec = importlib.util.spec_from_file_location("migration_0040_add_outfit_favorites", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _OpRecorder:
    def __init__(self) -> None:
        self.created_tables: dict[str, tuple[Any, ...]] = {}
        self.created_indexes: list[tuple[str, str, list[str]]] = []
        self.dropped_indexes: list[tuple[str, str]] = []
        self.dropped_tables: list[str] = []
        self.executed: list[str] = []

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def create_table(self, name: str, *elements: Any) -> None:
        self.created_tables[name] = elements

    def create_index(self, name: str, table_name: str, columns: list[str]) -> None:
        self.created_indexes.append((name, table_name, columns))

    def drop_index(self, name: str, table_name: str) -> None:
        self.dropped_indexes.append((name, table_name))

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)


def test_outfit_favorites_migration_upgrade_and_downgrade(monkeypatch: Any) -> None:
    migration = _load_migration()
    recorder = _OpRecorder()
    monkeypatch.setattr(migration, "op", recorder)

    assert migration.revision == "0040_add_outfit_favorites"
    assert migration.down_revision == "0039_shopping_sense_check_object_key"

    migration.upgrade()

    assert "outfit_favorites" in recorder.created_tables
    columns = [
        element.name
        for element in recorder.created_tables["outfit_favorites"]
        if isinstance(element, sa.Column)
    ]
    assert columns == ["user_id", "outfit_id", "created_at"]
    assert (
        "ix_outfit_favorites_user_id",
        "outfit_favorites",
        ["user_id"],
    ) in recorder.created_indexes
    assert (
        "ix_outfit_favorites_outfit_id",
        "outfit_favorites",
        ["outfit_id"],
    ) in recorder.created_indexes

    migration.downgrade()

    assert ("ix_outfit_favorites_outfit_id", "outfit_favorites") in recorder.dropped_indexes
    assert ("ix_outfit_favorites_user_id", "outfit_favorites") in recorder.dropped_indexes
    assert recorder.dropped_tables == ["outfit_favorites"]

