"""V3-011 — avatar profile, source-asset and version persistence.

Three additive tables (V3_SPEC §11.1), gated for exposure by
`ENABLE_V3_AVATAR`. Rows persist regardless of the flag — the V3-003 /
`ShoppingSenseCheck` convention — so flipping the flag back is a clean
rollback with no data loss.

**Three separate records, deliberately** (V3-011 acceptance 1). The photo, the
permission and the derived model are different things with different
lifetimes, and collapsing them is how a privacy boundary gets lost:

- `avatar_source_assets` — the material. One row per candidate photo, carrying
  its own consent state, because "consent is a **separate explicit action per
  source asset**; importing clothes never implies person-reference consent"
  (contract §4.3). Deleting the derived avatar does not retroactively un-take
  the photo, and purging the photo at retention does not invalidate the
  avatar built from it.
- `avatar_profiles` — the user's single, current avatar state, plus the
  consent under which it currently operates.
- `avatar_versions` — derived canonical assets. A new version is *added*, then
  swapped in; an old one is retired rather than mutated, so a failed `replace`
  leaves the previous version active and "there is never a gap where a ready
  avatar silently disappears" (contract §4.2).

**Durable truth, not signed URLs** (acceptance 2, V3_SPEC §13.5). Every image
reference here is a stable storage `object_key`; nothing signed is persisted.
Read paths sign on demand through `ObjectStoragePort.sign_render_url`, the
`WardrobeItemImage` / `PurchaseCandidate` precedent. This is what lets the
frozen `preview_url` / `canonical_front_url` fields be TTL-bound without any
row ever holding an expiring value.

**Naming reconciliation with V3-004.** The frozen `GET /v1/avatar-profile`
shape calls the candidate's identifier `source_asset_id: "uuid"` and the
consent route is `POST /v1/avatar-candidates/{source_asset_id}/consent` — so
here `AvatarSourceAsset.id` **is** the contract's `source_asset_id`, and the
storage key is a separate column. In V3-004 the same contract field name
(§5.2) denotes an object-key string, because purchase candidates have no such
table. Same words, two features, two referents; both are correct against their
own frozen section and neither is drift.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.common.enums import (
    AVATAR_STATUSES_WITH_ACTIVE_VERSION,
    AvatarConsentStatus,
    AvatarQualityStatus,
    AvatarSourceType,
    AvatarStatus,
)
from app.infrastructure.persistence.models.base import (
    Base,
    JsonDict,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


def _in_sql(enum_cls: type[StrEnum]) -> str:
    """Render an enum as a SQL `IN (...)` list so CHECKs can't drift from code."""
    return ", ".join(f"'{member.value}'" for member in enum_cls)


def _active_version_sql() -> str:
    """`status ∈ {ready, refresh_recommended}` ⇔ `active_version_id IS NOT NULL`.

    Generated from `AVATAR_STATUSES_WITH_ACTIVE_VERSION` so the constraint and
    the frozen response shape (contract §4.2: `active_version` is "null unless
    status is ready/refresh_recommended") cannot drift apart.
    """
    statuses = ", ".join(f"'{member.value}'" for member in sorted(AVATAR_STATUSES_WITH_ACTIVE_VERSION))
    return f"(status IN ({statuses})) = (active_version_id IS NOT NULL)"


class AvatarSourceAsset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One piece of candidate person-reference material, and its consent state.

    The row exists as soon as material is *noticed*; it says nothing about
    permission. `consent_status` starts `pending` and only an explicit user
    action moves it — V3-012's detection may create rows freely without ever
    being able to grant consent (its acceptance 1).
    """

    __tablename__ = "avatar_source_assets"
    __table_args__ = (
        Index(
            "ix_avatar_source_assets_user_id_created_at",
            "user_id",
            "created_at",
        ),
        # One row per underlying photo per user. This is what makes
        # "每张候选只提示一次" (V3_SPEC §7.3) and "accept=false ... the same
        # asset is never re-prompted" (contract §4.2) structural: re-detecting
        # a photo the user already declined cannot mint a fresh `pending` row,
        # it collides. Without this the prompt-once rule would be a convention
        # V3-012 has to remember.
        UniqueConstraint("user_id", "source_object_key", name="one_row_per_source_object"),
        CheckConstraint(
            f"source_type IN ({_in_sql(AvatarSourceType)})",
            name="source_type_enum",
        ),
        CheckConstraint(
            f"consent_status IN ({_in_sql(AvatarConsentStatus)})",
            name="consent_status_enum",
        ),
        CheckConstraint(
            f"quality_status IN ({_in_sql(AvatarQualityStatus)})",
            name="quality_status_enum",
        ),
        # A decided consent always records *when*, and an acceptance always
        # records *which copy version* was accepted — the audit minimum behind
        # V3-013 acceptance 1, enforced here rather than promised there.
        CheckConstraint(
            "(consent_status = 'pending') = (consent_decided_at IS NULL)",
            name="decided_consent_has_timestamp",
        ),
        CheckConstraint(
            "(consent_version IS NULL) OR (consent_status = 'accepted')",
            name="consent_version_only_when_accepted",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Stable storage key — NEVER a signed URL (acceptance 2). Sensitive person
    # material: belongs on the private bucket, whose retention is configured by
    # `AVATAR_SOURCE_RETENTION_DAYS` and whose final duration is a privacy
    # human gate (see app/application/avatar/retention.py).
    source_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AvatarQualityStatus.PENDING.value
    )
    # Bounded, non-judgemental signals only — e.g. single-person, full-body
    # visibility, blur, occlusion. V3-012 owns the vocabulary and its acceptance
    # 3 forbids age, attractiveness or body scoring; nothing derived about a
    # body may be written here (V3_SPEC §13.5 bans logging body inferences, and
    # a persisted column is stronger than a log line).
    quality_signals: Mapped[JsonDict | None] = mapped_column(JSONB, nullable=True)
    consent_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AvatarConsentStatus.PENDING.value
    )
    # The exact consent copy version the user accepted, kept per asset so a
    # later copy revision cannot rewrite what was actually shown. Final copy is
    # a human gate (contract §4.3); implementations use placeholder copy keys.
    consent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consent_decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AvatarProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The user's current avatar state — at most one row per user."""

    __tablename__ = "avatar_profiles"
    __table_args__ = (
        # At most one avatar profile per user: `GET /v1/avatar-profile` is a
        # singleton resource, so two rows would make the response ambiguous.
        UniqueConstraint("user_id", name="one_profile_per_user"),
        CheckConstraint(
            f"status IN ({_in_sql(AvatarStatus)})",
            name="status_enum",
        ),
        # The frozen response shape, enforced by the database. Also the reason
        # the active version cannot be deleted out from under a ready profile:
        # `active_version_id` is ON DELETE SET NULL, and setting it NULL while
        # status is `ready` violates this CHECK, so the delete fails. Retiring
        # or disabling first is the only way through — which is exactly the
        # "no gap where a ready avatar silently disappears" rule (contract §4.2).
        CheckConstraint(_active_version_sql(), name="active_version_matches_status"),
        # `disabled` is the only state that carries a revocation timestamp.
        CheckConstraint(
            "(status = 'disabled') = (disabled_at IS NOT NULL)",
            name="disabled_status_has_timestamp",
        ),
        CheckConstraint(
            "(consent_version IS NULL) = (consented_at IS NULL)",
            name="consent_fields_are_paired",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=AvatarStatus.NONE.value)
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        # `use_alter` breaks the avatar_profiles ↔ avatar_versions cycle: both
        # `create_all` (which the test suite uses) and Alembic need to create
        # one table before the other, so this constraint is added afterwards.
        ForeignKey("avatar_versions.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    # The consent currently in force for this profile, mirrored from the source
    # asset the user accepted. Kept here rather than derived because the frozen
    # GET shape returns `consent` while `candidate` is null (status=ready), and
    # because a source photo may be purged at retention while the consent that
    # produced the avatar must remain auditable.
    consent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when the user revokes. Revocation blocks new generation immediately;
    # it does not erase history — existing decisions stay readable (§4.3).
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AvatarVersion(UUIDPrimaryKeyMixin, Base):
    """One derived set of canonical reference assets.

    No `TimestampMixin`: a version is written once and then only *retired*. An
    `updated_at` column would imply the generated assets can be revised in
    place, which is precisely what the swap-don't-mutate rule forbids.
    """

    __tablename__ = "avatar_versions"
    __table_args__ = (
        Index(
            "ix_avatar_versions_avatar_profile_id_created_at",
            "avatar_profile_id",
            "created_at",
        ),
        CheckConstraint(
            f"quality_status IN ({_in_sql(AvatarQualityStatus)})",
            name="quality_status_enum",
        ),
    )

    avatar_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("avatar_profiles.id", ondelete="CASCADE"), nullable=False
    )
    # Provenance as a JSONB list of source-asset uuid strings, deliberately NOT
    # foreign keys: a source photo is minimum-necessary data and gets purged at
    # retention, and that purge must not cascade away the record of what this
    # version was built from. Same reasoning as `wardrobe_decisions.outfit_id`.
    source_asset_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    # Stable object keys again — the two canonical renders. Side is optional:
    # V3_SPEC §12.2 lists side view as an optional enhancement, not a P0 need.
    canonical_front_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_side_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AvatarQualityStatus.PENDING.value
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # NULL while this version is the live one. Set on swap-out, which is what
    # makes `replace` reversible: the retired row and its canonical assets are
    # still there until the retention sweep removes them.
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
