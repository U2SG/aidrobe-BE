"""V3-026 — the durable ledger a paid visual generation is admitted through.

One additive table. It exists because admission was previously *derived* from
`async_jobs` counts, and two things a derived count cannot express turned out to
be exactly the two production gaps V3-017 recorded:

1. **A newly queued job is paid usage.** `cost_guard.usage_today`'s call-count
   mode counts `succeeded`/`running` only, and `queued` is a *persistent* state
   — a job sits there until a drain tick claims it (`MAX_DRAIN_PER_TICK=5`,
   cron-paced). Sequential requests issued faster than the drain each counted
   zero and were each admitted, with no concurrency required. A reservation is
   held from the moment of admission, so the window closes by construction
   rather than by the worker keeping up.
2. **A revive is a fresh purchase and has to be counted as one.** Reviving a
   dead job inserts no row, so nothing derived from `async_jobs` can tell the
   first press from the fiftieth. `revive_index` is what makes the press
   countable, which is what lets it be bounded (V3-017's own docstring named a
   per-generation revive cap as the fix and recorded that no criterion of its
   own reached it).

**Provider-neutral on purpose.** `job_type` is the quota bucket
(`cost_guard._limit_for`'s existing name), and there is no provider column, no
model name and no price anywhere on this row — V3-026's description forbids
provider-specific pricing or model names in the domain contract, and a future
live-session adapter must be able to reserve through the same table. Render
provenance already lives on `outfit_visual_assets` (V3-016) and is joined when
an evaluation needs it.

**`reservation_key` + `usage_day` is the unit of purchase, and it is unique.**
For a first admission the key is the job's idempotency key — contract §6.3's
`static_tryon:{sha256(cache_key)}` — so the reservation, the asset and the job
are 1:1 by construction. For revive *n* it is that key plus `#revive:{n}`, so
each press is its own row and a replayed request cannot double-charge.

The **day** is part of it because the budget is daily: the unit is "this
generation, on this day". Keyed on the string alone the table could hold at most
one row per generation *ever*, so a later day's request for the same key would
replay a settled row and consume nothing — which would make `usage_day`
decorative for that key rather than the thing the budget is drawn against. See
`paid_generation_admission.reserve` for how narrow the *paid* consequence is
today, stated there rather than oversold here.

The unique index is the database's statement of that, not a hint: two rows for
one purchase would mean the admission lock's namespace is wrong, which is a bug
and must surface as `23505` rather than be swallowed by an `ON CONFLICT`.

**`outcome IS NULL` means held.** See `PaidGenerationOutcome` for why the state
machine is one column, and `ck_paid_generation_reservations_settled_pairing`
for the invariant that a settled row always carries both its outcome and its
timestamp.

**FK directions, each for a stated reason.**

- `user_id → users.id ON DELETE CASCADE` — the same rule V3-024 acceptance 5
  needs: `app.application.users.clear_data` sweeps columns with an FK to
  `users.id`, so this table is covered by clear-user-data by default rather
  than by someone remembering to list it.
- `job_id → async_jobs.id ON DELETE SET NULL` and
  `target_id → outfit_visual_assets.id ON DELETE SET NULL` — the opposite
  direction from the feedback table's, and deliberately. `DELETE
  /v1/avatar-profile?purge_derived=true` deletes try-on assets in bulk Core
  SQL; the *likeness* must go, but the record that the day's budget was spent
  must not, or a user could clear their spend by deleting the renders it paid
  for. So the reference is severed and the row survives.
"""

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.common.enums import PaidGenerationOutcome
from app.infrastructure.persistence.models.base import (
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


def _in_sql(enum_cls: type[StrEnum]) -> str:
    """Render an enum as a SQL `IN (...)` list so CHECKs can't drift from code."""
    return ", ".join(f"'{member.value}'" for member in enum_cls)


class PaidGenerationReservation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One admitted paid generation attempt-set, per user per day per bucket."""

    __tablename__ = "paid_generation_reservations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: The quota bucket — `cost_guard._limit_for`'s job type, never a provider.
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The UTC day the budget is drawn from, stored as a DATE rather than
    #: derived from `created_at` at read time: the admission predicate is then
    #: a plain equality an index can serve, and the day a reservation belongs
    #: to cannot shift with a later timezone or clock decision.
    usage_day: Mapped[date] = mapped_column(Date, nullable=False)
    #: The unit of purchase (module docstring). Unique.
    reservation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 0 for the first admission of a generation, n for its n-th revive. What
    #: makes the revive cap countable and durable.
    revive_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: How many provider attempts this reservation buys — `MAX_ATTEMPTS` at the
    #: time of admission (acceptance 4). Recorded rather than re-derived so a
    #: later constant change cannot rewrite what an old day was granted.
    attempt_grant: Mapped[int] = mapped_column(Integer, nullable=False)
    #: What the work actually used, written at settlement from `async_jobs
    #: .attempts`. NULL while held.
    attempts_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Quality-gate regenerations inside this reservation (V3-018's
    #: `quality_signals.render_count` minus the first render). The other half of
    #: acceptance 6: `outcome` says which kind of stop, this says how many
    #: re-renders the quality loop cost before it.
    quality_regenerations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("async_jobs.id", ondelete="SET NULL"), nullable=True
    )
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outfit_visual_assets.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            f"outcome IS NULL OR outcome IN ({_in_sql(PaidGenerationOutcome)})",
            name="outcome_enum",
        ),
        # A settled row carries both halves or neither. Without this, a partial
        # settlement could leave a row that reads as held to the admission
        # predicate and as finished to an operator.
        CheckConstraint(
            "(outcome IS NULL) = (settled_at IS NULL)",
            name="settled_pairing",
        ),
        CheckConstraint("revive_index >= 0", name="revive_index_non_negative"),
        CheckConstraint("attempt_grant >= 1", name="attempt_grant_positive"),
        # The admission read: every reservation this user holds in this bucket
        # today, which is the only query on the request path.
        Index(
            "ix_paid_generation_reservations_bucket",
            "user_id",
            "job_type",
            "usage_day",
        ),
        # The revive-cap read, and the settlement sweep's join key.
        Index("ix_paid_generation_reservations_target_id", "target_id"),
        # One row per unit of purchase, and the unit is (what, which day) —
        # see `reservation_key` above.
        Index(
            "uq_paid_generation_reservations_reservation_key",
            "reservation_key",
            "usage_day",
            unique=True,
        ),
    )
