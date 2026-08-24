"""V3-026 — the admission predicate, the revive predicate, and 0052's parity.

Everything here runs with **no database**: `uv run pytest --noconftest
tests/unit/test_v3_paid_generation_admission_sql.py`. That matters because the
behavioural half of this feature is testcontainers-backed and the workspace
manual bans those runs as local evidence on this host, so these assertions are
the only ones a reader can re-run without CI. The V3-005/V3-006 pattern.

Two of them read **compiled SQL text**, so they are coupled by design to how
SQLAlchemy renders bound parameters and `IN` clauses. A red here after a
dependency bump means the rendering changed, not that the predicate regressed —
check the assertion before the code.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import uuid
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa

from app.application import paid_generation_admission as admission
from app.common.enums import PaidGenerationOutcome
from app.infrastructure.persistence.models.paid_generation import PaidGenerationReservation

MIGRATION_PATH = Path("alembic/versions/0052_add_paid_generation_reservations.py")
TABLE = "paid_generation_reservations"

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TARGET = uuid.UUID("22222222-2222-2222-2222-222222222222")
_DAY = date(2026, 8, 24)


def _compiled(statement: Any) -> str:
    return re.sub(
        r"\s+",
        " ",
        str(statement.compile(dialect=sa.dialects.postgresql.dialect(), compile_kwargs={"literal_binds": True})),
    )


# --- what counts against the budget ----------------------------------------


def test_held_reservations_count_against_the_budget() -> None:
    """The over-admission fix, at its root: a queued generation is not free.

    `cost_guard`'s call-count mode counted `succeeded`/`running` only, and
    `queued` is a persistent state — that is the whole gap. The predicate
    therefore counts a row with NO outcome, which is what "held" is.
    """
    sql = _compiled(admission.counted_reservations_statement(_USER, "tryon", _DAY))
    assert "outcome IS NULL" in sql


def test_the_two_refunded_outcomes_are_absent_and_the_two_charged_ones_present() -> None:
    """Both directions, because either mistake is expensive in a different way.

    Charging `technical_failure` reverses V3-017 acceptance 3 (a provider outage
    would lock the user out of the retry they are about to press). Not charging
    `quality_rejected` would make a render we DID pay for free, because V3-018
    marks a rejection as a succeeded job.
    """
    assert set(admission.COUNTED_OUTCOMES) == {
        PaidGenerationOutcome.SUCCEEDED.value,
        PaidGenerationOutcome.QUALITY_REJECTED.value,
    }
    sql = _compiled(admission.counted_reservations_statement(_USER, "tryon", _DAY))
    assert "'succeeded'" in sql
    assert "'quality_rejected'" in sql
    assert "'technical_failure'" not in sql
    assert "'abandoned'" not in sql


def test_the_budget_is_scoped_to_one_user_bucket_and_day() -> None:
    sql = _compiled(admission.counted_reservations_statement(_USER, "tryon", _DAY))
    assert str(_USER) in sql
    assert "'tryon'" in sql
    assert "2026-08-24" in sql
    assert "count(*)" in sql.lower()


# --- the revive cap ---------------------------------------------------------


def test_the_revive_count_ignores_outcome_entirely() -> None:
    """A refund must not hand back a retry allowance.

    If this predicate filtered on outcome, a revive that was released (its
    conditional UPDATE lost) or that failed would stop counting — and the cap
    would reset every time the loop it exists to bound went round again.
    """
    sql = _compiled(admission.revive_count_statement(_TARGET))
    assert "outcome" not in sql
    assert "revive_index >= 1" in sql
    assert str(_TARGET) in sql


def test_the_first_admission_is_not_a_revive() -> None:
    """`revive_index=0` is the original purchase, so `>= 1` is the boundary.

    Off-by-one here would spend the whole cap before the user ever pressed
    retry, or never spend it at all.
    """
    sql = _compiled(admission.revive_count_statement(_TARGET))
    assert "revive_index >= 1" in sql
    assert "revive_index >= 0" not in sql


# --- the lock that makes admission atomic ----------------------------------


def test_the_bucket_lock_key_is_stable_and_bucket_scoped() -> None:
    other_user = uuid.UUID("33333333-3333-3333-3333-333333333333")
    key = admission.bucket_lock_key(_USER, "tryon", _DAY)

    assert key == admission.bucket_lock_key(_USER, "tryon", _DAY)
    assert key != admission.bucket_lock_key(other_user, "tryon", _DAY)
    assert key != admission.bucket_lock_key(_USER, "live_session", _DAY)
    assert key != admission.bucket_lock_key(_USER, "tryon", date(2026, 8, 25))
    # Postgres advisory locks take a bigint.
    assert -(2**63) <= key < 2**63


def test_the_lock_namespace_is_disjoint_from_the_recommendation_lock() -> None:
    """Advisory-lock keys are global to the database.

    `recommendation_repo._lock_daily_write` hashes `daily:{user}:{date}`. Without
    this module's own prefix, two unrelated features would serialise against
    each other on the same user and day — a latency bug nothing would explain.
    """
    import hashlib

    daily = hashlib.sha1(f"daily:{_USER}:{_DAY.isoformat()}".encode()).digest()[:8]
    assert admission.bucket_lock_key(_USER, "tryon", _DAY) != int.from_bytes(
        daily, "big", signed=True
    )


def test_reserve_takes_the_lock_before_it_reads_or_writes() -> None:
    """The ordering IS the atomicity, so it is asserted rather than described.

    Reads the function's source: a count-then-insert with the lock taken
    afterwards would compile, pass every behavioural test that is not concurrent,
    and over-admit under load.
    """
    source = inspect.getsource(admission.reserve)
    lock_at = source.index("_lock_bucket(")
    assert lock_at < source.index("counted_reservations_today("), "budget read before lock"
    assert lock_at < source.index("session.add("), "insert before lock"
    assert lock_at < source.index("reservation_key =="), "idempotency read before lock"


def test_reserve_does_not_absorb_a_duplicate_key() -> None:
    """Inside the bucket lock a duplicate reservation_key cannot be a race.

    `job_runtime.enqueue` uses `ON CONFLICT DO NOTHING` because its uniqueness IS
    contended; here the same construct would hide a wrong lock namespace, which
    is exactly the bug that would silently restore over-admission.
    """
    source = inspect.getsource(admission.reserve)
    assert "on_conflict" not in source


# --- settlement, and the join acceptance 6 depends on ----------------------


def test_settlement_reads_the_asset_and_not_only_the_job() -> None:
    """V3-018 makes a quality rejection a SUCCEEDED job, on purpose.

    So `AsyncJob.status` alone collapses `succeeded` and `quality_rejected` into
    one bucket and acceptance 6's distinction dies silently — with a green suite,
    because nothing else reads these columns.
    """
    source = inspect.getsource(admission.settle_terminal_reservations)
    assert "OutfitVisualAsset.generation_status" in source
    assert "outerjoin" in source
    assert admission._ASSET_STATUS_REJECTED == "rejected_by_quality_gate"


def test_settlement_only_ever_matches_a_held_row() -> None:
    """Idempotence, which is what makes the sweep safe to run from two places
    (the drain tail and its own cron) and safe to crash halfway through."""
    source = inspect.getsource(admission.settle_terminal_reservations)
    assert source.count("PaidGenerationReservation.outcome.is_(None)") == 2, (
        "both the SELECT and the UPDATE must be held-only"
    )


def test_a_failed_job_is_terminal_for_settlement() -> None:
    """`RequestStaticPreview._serve_cached` treats `failed` as revivable, i.e.
    terminal from the request path's view. Omitting it here would strand such a
    reservation held forever, holding budget nobody can spend."""
    assert set(admission._TERMINAL_JOB_STATUSES) == {"succeeded", "failed", "dead"}
    assert "running" not in admission._TERMINAL_JOB_STATUSES
    assert "queued" not in admission._TERMINAL_JOB_STATUSES


def test_regenerations_are_derived_from_render_count_defensively() -> None:
    """`render_count` is 1 for a render with no regeneration, and rows that
    predate V3-018 carry no signals at all."""
    assert admission._regenerations(None) == 0
    assert admission._regenerations({}) == 0
    assert admission._regenerations({"render_count": 1}) == 0
    assert admission._regenerations({"render_count": 2}) == 1
    # Not an int, or nonsense: 0 rather than a crash inside a cron sweep.
    assert admission._regenerations({"render_count": "2"}) == 0
    assert admission._regenerations({"render_count": 0}) == 0


# --- migration 0052 vs the model -------------------------------------------


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0052", MIGRATION_PATH)
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

    def create_index(
        self, name: str, table_name: str, columns: list[str], unique: bool = False
    ) -> None:
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
    assert migration.revision == "0052_add_paid_generation_reservations"
    assert migration.down_revision == "0051_add_static_preview_feedback"


def test_columns_match_the_model(monkeypatch: Any) -> None:
    """`tests/conftest.py` builds the test schema with `create_all`, so a
    divergence between these two descriptions is invisible to every other test
    in the suite — CI would be green against a schema production never gets."""
    recorder = _run_upgrade(monkeypatch)

    assert set(recorder.created_tables) == {TABLE}
    migrated = {
        element.name
        for element in recorder.created_tables[TABLE]
        if isinstance(element, sa.Column)
    }
    assert migrated == {column.name for column in PaidGenerationReservation.__table__.columns}


def test_check_constraints_match_the_model(monkeypatch: Any) -> None:
    recorder = _run_upgrade(monkeypatch)

    migrated = {
        _normalize(element.sqltext)
        for element in recorder.created_tables[TABLE]
        if isinstance(element, sa.CheckConstraint)
    }
    modelled = {
        _normalize(constraint.sqltext)
        for constraint in PaidGenerationReservation.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert migrated == modelled
    # The enum CHECK really is the vocabulary, not a stale copy of it.
    enum_check = next(check for check in migrated if "outcome IN" in check)
    for member in PaidGenerationOutcome:
        assert f"'{member.value}'" in enum_check
    # And the state machine is one column: a settled row carries both halves.
    assert any("(outcome IS NULL) = (settled_at IS NULL)" in check for check in migrated)


def test_indexes_match_the_model(monkeypatch: Any) -> None:
    recorder = _run_upgrade(monkeypatch)

    migrated = {
        (name, table, tuple(columns), unique)
        for name, table, columns, unique in recorder.created_indexes
    }
    modelled = {
        (index.name, TABLE, tuple(column.name for column in index.columns), bool(index.unique))
        for index in PaidGenerationReservation.__table__.indexes
    }
    assert migrated == modelled
    # One row per unit of purchase — the database's statement of it.
    assert (
        "uq_paid_generation_reservations_reservation_key",
        TABLE,
        ("reservation_key",),
        True,
    ) in migrated


def test_the_spend_record_outlives_the_render_it_paid_for(monkeypatch: Any) -> None:
    """`purge_derived=true` deletes try-on assets in bulk Core SQL. The likeness
    must go; the record that the budget was spent must not, or deleting your
    renders would reset your daily spend."""
    recorder = _run_upgrade(monkeypatch)

    ondelete = {
        tuple(constraint.column_keys): constraint.ondelete
        for constraint in recorder.created_tables[TABLE]
        if isinstance(constraint, sa.ForeignKeyConstraint)
    }
    assert ondelete == {
        ("user_id",): "CASCADE",
        ("job_id",): "SET NULL",
        ("target_id",): "SET NULL",
    }


def test_upgrade_is_additive_only(monkeypatch: Any) -> None:
    """No destructive-migration human gate is needed precisely because nothing
    existing is touched — `async_jobs` in particular."""
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
    assert {name for name, _t, _c, _u in recorder.created_indexes} == {
        name for name, _t in recorder.dropped_indexes
    }
