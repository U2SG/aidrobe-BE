"""V3-024 — record one fidelity verdict for one static try-on render.

The write side of contract §6.1's
`POST /v1/outfit-visual-assets/{asset_id}/feedback`. The endpoint has been
frozen since the contract was written and V3-020 already ships a client that
calls it; what was missing was an owner. Everything that decides *whether* a
verdict may be recorded lives here, so the router is a translation of one
closed outcome vocabulary onto §3's envelope.

**Ownership, existence and type are one query, deliberately.** The asset must
be this user's AND `visual_type='static_tryon'`, and every failure of that
conjunction answers the same 404:

- someone else's asset ⇒ 404, never 403 (contract §3 — a 403 would confirm the
  id exists);
- a missing asset ⇒ 404;
- the caller's *own* `ootd_poster` / `outfit_board` row ⇒ also 404, because
  §6.1's feedback resource is defined on try-on renders. The three-verdict
  vocabulary is meaningless for a flat-lay board ("does it look like me?"), so
  answering anything but "no such feedback surface here" would be the first
  step towards a generic image-feedback endpoint (BE#154 names this risk
  explicitly).

Writing it as one WHERE clause rather than a fetch-then-branch means no code
path can accidentally distinguish the three on the wire.

**Idempotency, and the one documented conflict rule: last write wins.**
`static_preview_feedback` is unique on `asset_id`, and this upserts onto that
index:

- *Repeated identical submission* — the `ON CONFLICT DO UPDATE` carries a
  `WHERE` that is false when nothing changed, so the row is not rewritten at
  all: same id, same `created_at`, same `updated_at`. The caller still gets
  200 with the stored verdict. "No-op" is literal, not approximate.
- *Conflicting resubmission* — the stored verdict is **replaced** and
  `updated_at` moves. A user who taps `looks_like_me` and then `garment_wrong`
  has changed their mind, and the honest record of that is one current answer
  plus the timestamp it was given, not two rows inflating both rates. Rejecting
  the second tap with a 409 was the other option §6.1's issue allows; it was
  not taken because the surface is fire-and-forget (a refusal is invisible to
  the user, so they would believe the new answer was recorded) and because
  V3-020's client-side single-shot lock can then be relaxed against a server
  that dedupes, rather than the reverse.

Either way exactly one row per render reaches the metrics query, which is what
"does not double-count" has to mean for a rate.

No check-then-insert guard: a `23505` here would be a race, not a bug, and the
`ON CONFLICT` is what resolves it — the `request_static_preview` precedent.

**`aspects` is normalized before it is compared or stored.** Sorted and
deduplicated, so `["color","face"]` and `["face","color","face"]` are the same
answer and a re-tap in a different order stays a no-op instead of registering
as a change of mind. `None` (the key omitted, which is what V3-020 sends) and
`[]` remain distinct — the frozen body makes the field optional, and "did not
elaborate" is not "chose nothing".

**This module emits no analytics event, on purpose.**
`docs/v3-analytics-events.md` assigns `v3_preview_feedback` to **fe** and says
the owner is the only side allowed to emit it — "a duplicate emit from the
other side would double-count every rate below". The BE's durable record and
the FE's event are two channels for the same tap by design, and only one of
them counts.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import VisualType
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.preview_feedback import StaticPreviewFeedback

logger = structlog.get_logger(__name__)


class FeedbackTargetNotFound(Exception):
    """No `static_tryon` asset with this id belongs to this user.

    One exception for all three causes (missing / foreign / wrong visual type)
    so the router cannot tell them apart even by accident — see the module
    docstring.
    """


@dataclass(frozen=True)
class FeedbackRecorded:
    feedback_id: uuid.UUID
    verdict: str
    aspects: tuple[str, ...] | None
    recorded_at: datetime
    #: False when this submission matched the stored one exactly and no row was
    #: written. Not part of any response shape — it exists so the log (and the
    #: idempotency test) can tell a no-op from a real write.
    changed: bool


def normalize_aspects(aspects: Sequence[str] | None) -> list[str] | None:
    """Sorted, deduplicated — or `None` when the key was omitted."""
    if aspects is None:
        return None
    return sorted(set(aspects))


class RecordPreviewFeedback:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def execute(
        self,
        *,
        user_id: uuid.UUID,
        asset_id: uuid.UUID,
        verdict: str,
        aspects: Sequence[str] | None = None,
    ) -> FeedbackRecorded:
        owned = await self.session.scalar(
            select(OutfitVisualAsset.id).where(
                OutfitVisualAsset.id == asset_id,
                OutfitVisualAsset.user_id == user_id,
                OutfitVisualAsset.visual_type == VisualType.STATIC_TRYON.value,
            )
        )
        if owned is None:
            raise FeedbackTargetNotFound

        normalized = normalize_aspects(aspects)
        recorded = await self._upsert(
            user_id=user_id, asset_id=asset_id, verdict=verdict, aspects=normalized
        )

        # The verdict and whether anything moved. Never the asset's provenance,
        # never a URL (contract §7) — and `asset_id` only because it is already
        # in the request path.
        logger.info(
            "v3.preview_feedback.recorded",
            asset_id=str(asset_id),
            verdict=verdict,
            aspect_count=len(normalized) if normalized is not None else 0,
            changed=recorded.changed,
        )
        return recorded

    async def _upsert(
        self,
        *,
        user_id: uuid.UUID,
        asset_id: uuid.UUID,
        verdict: str,
        aspects: list[str] | None,
    ) -> FeedbackRecorded:
        insert_stmt = pg_insert(StaticPreviewFeedback).values(
            id=uuid.uuid4(),
            user_id=user_id,
            asset_id=asset_id,
            verdict=verdict,
            aspects=aspects,
        )
        upsert = (
            insert_stmt.on_conflict_do_update(
                index_elements=[StaticPreviewFeedback.asset_id],
                set_={
                    "verdict": insert_stmt.excluded.verdict,
                    "aspects": insert_stmt.excluded.aspects,
                    # TimestampMixin's `onupdate` is an ORM-level hook and this
                    # is Core SQL, so the bump is explicit. It is the only
                    # column that says when the user changed their mind.
                    "updated_at": func.now(),
                },
                # False for an identical resubmission ⇒ no row is written and
                # `RETURNING` yields nothing (handled below), which is what
                # makes the no-op observable rather than merely harmless.
                where=or_(
                    StaticPreviewFeedback.verdict.is_distinct_from(
                        insert_stmt.excluded.verdict
                    ),
                    StaticPreviewFeedback.aspects.is_distinct_from(
                        insert_stmt.excluded.aspects
                    ),
                ),
            )
            .returning(
                StaticPreviewFeedback.id,
                StaticPreviewFeedback.verdict,
                StaticPreviewFeedback.aspects,
                StaticPreviewFeedback.updated_at,
            )
        )
        row = (await self.session.execute(upsert)).first()
        if row is None:
            # The suppressed-update branch: the stored answer already matches,
            # so read it back and report the same success. A missing row here
            # is impossible (the conflict fired), but `one_or_none` + an
            # explicit raise beats a `NoResultFound` surfacing as a 500 on the
            # exact path acceptance 3 is about.
            row = (
                await self.session.execute(
                    select(
                        StaticPreviewFeedback.id,
                        StaticPreviewFeedback.verdict,
                        StaticPreviewFeedback.aspects,
                        StaticPreviewFeedback.updated_at,
                    ).where(StaticPreviewFeedback.asset_id == asset_id)
                )
            ).one()
            changed = False
        else:
            changed = True

        feedback_id, stored_verdict, stored_aspects, updated_at = row
        return FeedbackRecorded(
            feedback_id=feedback_id,
            verdict=stored_verdict,
            aspects=tuple(stored_aspects) if stored_aspects is not None else None,
            recorded_at=updated_at,
            changed=changed,
        )
