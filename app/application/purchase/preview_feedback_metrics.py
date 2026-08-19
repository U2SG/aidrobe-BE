"""V3-024 — fidelity rates per provider / model / prompt version.

V3-024 acceptance 6 and BE#154's evaluation section: the persisted signal must
make "looks-like-me rate" and "garment-wrong rate" computable **per pipeline
version**, as inspectable dimensions rather than one opaque quality score. This
module is that query, and the shape of its result is the point:

- the grouping key is the whole provenance triple
  (`provider`, `model_version`, `prompt_version`), so a prompt change and a
  model change are never averaged together;
- every verdict keeps its own count *and* its own rate, so a reader can see the
  denominator that produced a rate and check it;
- there is no composite, no weighting and no single "quality" number. A scalar
  would be the one artefact that lets a regression in identity fidelity be
  cancelled out by an improvement in garment rendering — which is exactly the
  failure V3_SPEC §20's kill criteria are written to catch.

**Server-side only.** Contract §6.4 keeps `provider` / `model_version` /
`prompt_version` internal, so this is a query function with no route. Nothing
in the public API exposes it and nothing should: the feedback response returns
the user's own verdict and no provenance at all.

**`ready_only` and the denominator.** By default only renders that reached
`ready` are counted. A verdict can in practice only be given on a render the
user saw, but the rate's *meaning* is "of the previews we actually showed, how
many looked right" — so the filter states that rather than relying on it, and
the parameter exists so an operator can widen the denominator deliberately
instead of by editing the query. `generation_status` distribution and
cache-hit-vs-generated are not computed here: those come from the
`v3_preview_outcome` analytics event, which already carries both fields
(docs/v3-analytics-events.md), and duplicating them from a second source would
let the two disagree.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import PreviewFeedbackVerdict, VisualType
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.preview_feedback import StaticPreviewFeedback

#: The status a render must have reached to be part of the default denominator.
READY = "ready"


@dataclass(frozen=True)
class VersionFidelity:
    """One provenance triple's counts and the rates derived from them."""

    provider: str | None
    model_version: str | None
    prompt_version: str | None
    total: int
    looks_like_me: int
    not_like_me: int
    garment_wrong: int

    @property
    def looks_like_me_rate(self) -> float:
        return _rate(self.looks_like_me, self.total)

    @property
    def not_like_me_rate(self) -> float:
        return _rate(self.not_like_me, self.total)

    @property
    def garment_wrong_rate(self) -> float:
        return _rate(self.garment_wrong, self.total)

    def as_row(self) -> dict[str, object]:
        """Flat, printable form — what an evaluation run reads."""
        return {
            "provider": self.provider,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "total": self.total,
            "looks_like_me": self.looks_like_me,
            "not_like_me": self.not_like_me,
            "garment_wrong": self.garment_wrong,
            "looks_like_me_rate": self.looks_like_me_rate,
            "not_like_me_rate": self.not_like_me_rate,
            "garment_wrong_rate": self.garment_wrong_rate,
        }


def _rate(count: int, total: int) -> float:
    """Zero verdicts is 0.0, never a division error and never `None`.

    A triple with no feedback yet does not appear in the result at all (the
    join produces no group), so `total == 0` is only reachable by constructing
    a `VersionFidelity` directly — a pure-function edge worth pinning rather
    than leaving to raise inside a report.
    """
    if total <= 0:
        return 0.0
    return count / total


def _verdict_count(verdict: PreviewFeedbackVerdict) -> ColumnElement[int]:
    """`count(*) FILTER (WHERE verdict = …)` — one pass, one row per group."""
    return func.count().filter(StaticPreviewFeedback.verdict == verdict.value)


async def static_preview_fidelity_by_version(
    session: AsyncSession, *, ready_only: bool = True
) -> list[VersionFidelity]:
    """Group every recorded verdict by the pipeline version that rendered it."""
    stmt = (
        select(
            OutfitVisualAsset.provider,
            OutfitVisualAsset.model_version,
            OutfitVisualAsset.prompt_version,
            func.count().label("total"),
            _verdict_count(PreviewFeedbackVerdict.LOOKS_LIKE_ME).label("looks_like_me"),
            _verdict_count(PreviewFeedbackVerdict.NOT_LIKE_ME).label("not_like_me"),
            _verdict_count(PreviewFeedbackVerdict.GARMENT_WRONG).label("garment_wrong"),
        )
        .select_from(StaticPreviewFeedback)
        .join(OutfitVisualAsset, OutfitVisualAsset.id == StaticPreviewFeedback.asset_id)
        .where(OutfitVisualAsset.visual_type == VisualType.STATIC_TRYON.value)
        .group_by(
            OutfitVisualAsset.provider,
            OutfitVisualAsset.model_version,
            OutfitVisualAsset.prompt_version,
        )
        .order_by(
            OutfitVisualAsset.provider,
            OutfitVisualAsset.model_version,
            OutfitVisualAsset.prompt_version,
        )
    )
    if ready_only:
        stmt = stmt.where(OutfitVisualAsset.generation_status == READY)

    return [
        VersionFidelity(
            provider=provider,
            model_version=model_version,
            prompt_version=prompt_version,
            total=total,
            looks_like_me=looks_like_me,
            not_like_me=not_like_me,
            garment_wrong=garment_wrong,
        )
        for (
            provider,
            model_version,
            prompt_version,
            total,
            looks_like_me,
            not_like_me,
            garment_wrong,
        ) in (await session.execute(stmt)).all()
    ]
