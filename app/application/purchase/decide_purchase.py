"""V3-007 — produce (or replay) the immutable purchase decision.

The DB half of the feature; every judgement lives in
`purchase_decision_rules.py`. This module gathers V3-005 and V3-006 evidence,
renders the decision once, and freezes it into `wardrobe_decisions`.

**Idempotent by index, not by check-then-insert.** Contract §5.3 lets P0 always
return the first decision for a candidate. The fast path reads it back; the
race is handled by the unique index V3-003 put on `candidate_id` — two
concurrent POSTs both compute, one wins the INSERT and the other catches
`IntegrityError` inside a SAVEPOINT and reads the winner. A "check then insert"
guard would be the 23505-as-a-bug antipattern: it narrows the window instead of
closing it, and it would let two decisions exist if it ever lost.

**Snapshot layout** (V3-003's columns, V3-006's executed key):

- `context_snapshot` — what the decision was computed *against*: the concern
  asked, the candidate's parsed attributes as of that moment, the candidate's
  parse status and the locale the copy was rendered in.
- `evidence_snapshot` — `closet_evidence` (V3-005), `outfit_evidence` (V3-006,
  the key `test_v3_outfit_evidence.py` already replays from) and `decision`
  (the rendered §5.3 payload the client is shown).

Nothing signed is stored: `image_url` is materialized at read time by the
router, so an expired URL can never be frozen into a historical explanation.

**Query budget** — constant in wardrobe size, the same N+1 guard V3-005/V3-006
pin (`tests/unit/test_v3_decision_query_count.py`):

- 1 — the existing-decision read (idempotency).
- 1 — the user's locale.
- 2–4 — V3-005 similarity (scan, scenes, and the embedding tier when enabled).
- 2 — V3-006 outfit composition (scan, scenes).

No provider call and no LLM: the decision is rules over evidence
(V3_SPEC §10.1), so a decision costs no quota and needs no cost guard.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.purchase.candidate_normalizer import ParsedItem
from app.application.purchase.compose_outfit_evidence import compose_outfit_evidence
from app.application.purchase.compute_similarity_evidence import compute_similarity_evidence
from app.application.purchase.purchase_decision_rules import (
    CONCERN_NONE,
    PurchaseDecisionOutcome,
    decide,
    replay_decision,
)
from app.common.enums import ImageType, WardrobeDecisionType
from app.common.locale import resolve_locale
from app.infrastructure.persistence.models.purchase_decision import (
    DECISION_SNAPSHOT_VERSION,
    PurchaseCandidate,
    WardrobeDecision,
)
from app.infrastructure.persistence.models.user import UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItemImage
from app.infrastructure.persistence.repositories.purchase_decision_repo import (
    SqlAlchemyWardrobeDecisionRepository,
)


def parsed_item_from_snapshot(raw: Any) -> ParsedItem | None:
    """`purchase_candidates.parsed_item` → `ParsedItem`, tolerantly.

    A degraded V3-004 parse writes a partial dict and `parse_failed` writes
    `None`; both must still be decidable (contract §5.2), so every field is
    read defensively and an unusable result is passed through as-is. The
    evidence modules turn it into their `unknown` states, which is how the
    decision reaches `insufficient_information` honestly.
    """
    if not isinstance(raw, dict):
        return None

    def _str(key: str) -> str | None:
        value = raw.get(key)
        return value if isinstance(value, str) and value.strip() else None

    def _list(key: str) -> list[str] | None:
        value = raw.get(key)
        if not isinstance(value, list):
            return None
        return [v for v in value if isinstance(v, str) and v.strip()]

    return ParsedItem(
        category=_str("category"),
        colors=_list("colors"),
        silhouette=_str("silhouette"),
        material_hints=_list("material_hints"),
        price=_str("price"),
        scene_tags=_list("scene_tags"),
        garment_type=_str("garment_type"),
    )


class DecidePurchase:
    """`POST /v1/purchase-candidates/{id}/decision` — the use case."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.decisions = SqlAlchemyWardrobeDecisionRepository(session)

    async def execute(
        self,
        user_id: uuid.UUID,
        candidate: PurchaseCandidate,
        *,
        concern: str = CONCERN_NONE,
    ) -> WardrobeDecision:
        """The existing decision for this candidate, or a newly frozen one."""
        existing = await self.decisions.get_for_candidate(candidate.id, user_id)
        if existing is not None:
            return existing

        parsed = parsed_item_from_snapshot(candidate.parsed_item)
        locale = await self._locale(user_id)
        closet = await compute_similarity_evidence(self.session, user_id, parsed)
        outfit = await compose_outfit_evidence(self.session, user_id, parsed, closet)
        outcome = decide(parsed, closet, outfit, concern=concern, locale=locale)

        decision = WardrobeDecision(
            user_id=user_id,
            decision_type=WardrobeDecisionType.PURCHASE.value,
            candidate_id=candidate.id,
            snapshot_version=DECISION_SNAPSHOT_VERSION,
            context_snapshot={
                "concern": concern,
                "parsed_item": parsed.as_payload() if parsed is not None else None,
                "candidate_status": candidate.status,
                "source_type": candidate.source_type,
                "locale": locale,
            },
            evidence_snapshot={
                "closet_evidence": closet.as_payload(),
                "outfit_evidence": outfit.as_payload(),
                "decision": outcome.as_payload(),
            },
            decision=outcome.decision,
            uncertainty=outcome.uncertainty,
            visual_need=outcome.visual_need,
        )
        return await self._add_or_replay(decision, candidate.id, user_id)

    async def _add_or_replay(
        self,
        decision: WardrobeDecision,
        candidate_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> WardrobeDecision:
        """Insert, or let the concurrent winner stand.

        The flush runs inside a SAVEPOINT: a bare `IntegrityError` would poison
        the session and abort the whole request, so the rollback has to be
        scoped to this one write (the `outfits/feedback.py` precedent).
        """
        try:
            async with self.session.begin_nested():
                await self.decisions.add(decision)
        except IntegrityError:
            winner = await self.decisions.get_for_candidate(candidate_id, user_id)
            if winner is None:
                # The unique index was not what rejected the row — do not
                # swallow a real integrity problem behind an empty read.
                raise
            return winner
        return decision

    async def _locale(self, user_id: uuid.UUID) -> str:
        """The locale the copy is rendered in, resolved once and frozen.

        Read from the user's settings the way `sense_check` does. The request
        body is frozen at `{concern}` (contract §5.3) so there is no per-call
        locale hint, and a decision keeps the language it was written in even
        if the user later switches — the copy is part of the immutable
        explanation, not a live render.
        """
        settings_locale = await self.session.scalar(
            select(UserSettings.locale).where(UserSettings.user_id == user_id)
        )
        return resolve_locale(None, settings_locale)


async def load_item_thumbnails(
    session: AsyncSession, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[str | None, str | None]]:
    """`item_id → (object_key, fallback_url)` for the comparison thumbnails.

    One `IN` query, `standard` preferred over `original` — the same shape as
    `sense_check._thumbnail_sources`, kept as a separate function rather than
    imported because that one is a private method of a live M17 use case (the
    same call V3-005 made about `_COMPLEMENTS`).

    Returns the durable pair; the presentation layer signs it at request time
    through `resolve_render_url`, so nothing TTL-bound is ever persisted.
    """
    if not item_ids:
        return {}
    images = list(
        await session.scalars(select(WardrobeItemImage).where(WardrobeItemImage.item_id.in_(item_ids)))
    )
    preferred: dict[uuid.UUID, WardrobeItemImage] = {}
    for image in images:
        current = preferred.get(image.item_id)
        if current is None or (
            current.image_type != ImageType.STANDARD.value and image.image_type == ImageType.STANDARD.value
        ):
            preferred[image.item_id] = image
    return {item_id: (image.object_key, image.image_url) for item_id, image in preferred.items()}


def shown_decision(decision: WardrobeDecision) -> PurchaseDecisionOutcome:
    """The §5.3 payload a stored decision renders as.

    Replayed from the snapshot rather than recomputed, so a decision explains
    itself the same way after the wardrobe it described has changed — which is
    the property `wardrobe_decisions` exists for.
    """
    snapshot = decision.evidence_snapshot
    return replay_decision(snapshot["decision"])


__all__ = [
    "DecidePurchase",
    "load_item_thumbnails",
    "parsed_item_from_snapshot",
    "shown_decision",
]
