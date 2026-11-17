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
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ask.styling import StylingService, conflict, fingerprint, lock_request
from app.application.purchase.candidate_normalizer import ParsedItem
from app.application.purchase.compose_outfit_evidence import compose_outfit_evidence
from app.application.purchase.compute_similarity_evidence import compute_similarity_evidence
from app.application.purchase.design_purchase import PurchaseDesign, contextualize_purchase, resolve_purchase_design
from app.application.purchase.purchase_decision_rules import (
    CONCERN_NONE,
    PurchaseDecisionOutcome,
    decide,
    replay_decision,
)
from app.common import error_codes
from app.common.enums import WardrobeDecisionType
from app.common.exceptions import FriendlyException
from app.common.locale import resolve_locale
from app.infrastructure.persistence.models.purchase_decision import (
    DECISION_SNAPSHOT_VERSION,
    PurchaseCandidate,
    WardrobeDecision,
)
from app.infrastructure.persistence.models.user import UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.purchase_decision_repo import (
    SqlAlchemyWardrobeDecisionRepository,
)
from app.infrastructure.persistence.repositories.wardrobe_repo import ImageSource, load_wardrobe_image_sources


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
        conversation_id: uuid.UUID | None = None,
        expected_revision: int | None = None,
        request_id: uuid.UUID | None = None,
    ) -> WardrobeDecision:
        """The existing decision for this candidate, or a newly frozen one."""
        if candidate.user_id != user_id:
            raise FriendlyException(error_codes.NOT_FOUND, "Purchase candidate not found", 404)
        context_values = (conversation_id, expected_revision, request_id)
        if any(value is not None for value in context_values) and any(value is None for value in context_values):
            raise FriendlyException(error_codes.VALIDATION_ERROR, "Incomplete design context", 422)
        design: PurchaseDesign | None = None
        request_hash: str | None = None
        if request_id is not None:
            await lock_request(self.session, user_id, request_id)
            request_hash = fingerprint(
                "purchase", candidate_id=candidate.id, concern=concern,
                conversation_id=conversation_id, expected_revision=expected_revision,
            )
            existing = await self.decisions.get_for_request(user_id, request_id)
            if existing is not None and existing.request_fingerprint != request_hash:
                raise conflict("request_id was already used for a different action")
        else:
            existing = await self.decisions.get_for_candidate(candidate.id, user_id)
        if existing is not None:
            return existing
        if request_id is not None and conversation_id is not None and expected_revision is not None:
            await StylingService(self.session, user_id).replay(request_id, request_hash or "")
            design = await resolve_purchase_design(self.session, user_id, conversation_id, expected_revision)

        parsed = parsed_item_from_snapshot(candidate.parsed_item)
        locale = await self._locale(user_id)
        closet = await compute_similarity_evidence(self.session, user_id, parsed)
        outfit = (
            await compose_outfit_evidence(self.session, user_id, parsed, closet, constraints=design.composition)
            if design is not None else await compose_outfit_evidence(self.session, user_id, parsed, closet)
        )
        outcome = decide(parsed, closet, outfit, concern=concern, locale=locale)
        design_evidence: dict[str, Any] | None = None
        if design is not None:
            outcome, design_evidence = await contextualize_purchase(
                self.session, user_id, design, parsed, outfit, outcome, locale,
            )

        decision = WardrobeDecision(
            user_id=user_id,
            decision_type=WardrobeDecisionType.PURCHASE.value,
            candidate_id=candidate.id,
            conversation_id=conversation_id,
            design_revision=expected_revision,
            request_id=request_id,
            request_fingerprint=request_hash,
            snapshot_version=DECISION_SNAPSHOT_VERSION,
            context_snapshot={
                "concern": concern,
                "parsed_item": parsed.as_payload() if parsed is not None else None,
                "candidate_status": candidate.status,
                "source_type": candidate.source_type,
                "locale": locale,
                **({
                    "design_context": design.snapshot,
                    "candidate_source": {
                        "source_type": candidate.source_type, "source_url": candidate.source_url,
                        "source_object_key": candidate.source_object_key,
                        "created_at": candidate.created_at.isoformat(),
                    },
                } if design is not None else {}),
            },
            evidence_snapshot={
                "closet_evidence": closet.as_payload(),
                "outfit_evidence": outfit.as_payload(),
                "decision": outcome.as_payload(),
                **({"design_evidence": design_evidence} if design_evidence is not None else {}),
            },
            decision=outcome.decision,
            uncertainty=outcome.uncertainty,
            visual_need=outcome.visual_need,
        )
        if design is not None:
            # now() in Postgres is the transaction start, which may precede a
            # forget operation we waited for. Use the actual frozen-context time.
            decision.created_at = datetime.fromisoformat(design.snapshot["captured_at"])
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
            winner = (
                await self.decisions.get_for_request(user_id, decision.request_id) if decision.request_id is not None
                else await self.decisions.get_for_candidate(candidate_id, user_id)
            )
            if winner is None:
                # The unique index was not what rejected the row — do not
                # swallow a real integrity problem behind an empty read.
                raise
            if winner.request_fingerprint != decision.request_fingerprint:
                raise conflict("request_id was already used for a different action") from None
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
    session: AsyncSession, user_id: uuid.UUID, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ImageSource]:
    if not item_ids:
        return {}
    items = (
        await session.scalars(
            select(WardrobeItem).where(WardrobeItem.id.in_(item_ids), WardrobeItem.user_id == user_id)
        )
    ).all()
    sources = await load_wardrobe_image_sources(session, items)
    return {item_id: cover for item_id, (cover, _) in sources.items()}


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
