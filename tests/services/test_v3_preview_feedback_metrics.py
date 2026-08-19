"""V3-024 acceptance 6 — fidelity rates per provider / model / prompt version.

The criterion is not "a number can be produced"; it is that looks-like-me and
garment-wrong are computable **per pipeline version** as *inspectable
dimensions, not one opaque score*. So this file seeds two provenance triples
that would average into a misleading middle, and asserts the query keeps them
apart with their own denominators.

`test_the_grouped_rows_are_printable_evidence` prints the result table. That
print is the feature's third `evidence_required` item: the CI log is where the
actual numbers can be read, since a green check alone shows no rows.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.purchase.preview_feedback_metrics import (
    VersionFidelity,
    static_preview_fidelity_by_version,
)
from app.common.enums import VisualType
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.preview_feedback import StaticPreviewFeedback
from app.infrastructure.persistence.models.user import User

PROVIDER = "fake-static-tryon"
MODEL = "compositor-1"


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    return user_id


async def _seed_verdict(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    verdict: str,
    prompt_version: str,
    provider: str = PROVIDER,
    model_version: str = MODEL,
    generation_status: str = "ready",
    visual_type: str = VisualType.STATIC_TRYON.value,
) -> uuid.UUID:
    is_tryon = visual_type == VisualType.STATIC_TRYON.value
    asset = OutfitVisualAsset(
        user_id=user_id,
        visual_type=visual_type,
        generation_status=generation_status,
        provider=provider if is_tryon else None,
        model_version=model_version if is_tryon else None,
        prompt_version=prompt_version if is_tryon else None,
    )
    session.add(asset)
    await session.flush()
    session.add(
        StaticPreviewFeedback(user_id=user_id, asset_id=asset.id, verdict=verdict)
    )
    await session.flush()
    return asset.id


async def _seed_two_prompt_versions(session: AsyncSession, user_id: uuid.UUID) -> None:
    """`p0-static-1` renders badly; `p0-static-2` fixes identity, not garments.

    Pooled, this is 50% looks-like-me and a story about nothing. Split by
    prompt version it says the change worked on one axis and not the other —
    which is the whole reason the dimensions stay separate.
    """
    for verdict in ("looks_like_me", "not_like_me", "not_like_me", "garment_wrong"):
        await _seed_verdict(session, user_id, verdict=verdict, prompt_version="p0-static-1")
    for verdict in ("looks_like_me", "looks_like_me", "looks_like_me", "garment_wrong"):
        await _seed_verdict(session, user_id, verdict=verdict, prompt_version="p0-static-2")


async def test_rates_are_computed_per_prompt_version(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    await _seed_two_prompt_versions(db_session, user_id)

    rows = {row.prompt_version: row for row in await static_preview_fidelity_by_version(db_session)}

    assert set(rows) == {"p0-static-1", "p0-static-2"}
    first, second = rows["p0-static-1"], rows["p0-static-2"]

    assert (first.total, first.looks_like_me, first.not_like_me, first.garment_wrong) == (4, 1, 2, 1)
    assert (second.total, second.looks_like_me, second.not_like_me, second.garment_wrong) == (4, 3, 0, 1)

    # Identity fidelity moved; garment fidelity did not. A single score would
    # have shown one number for both versions moving "up a bit".
    assert first.looks_like_me_rate == 0.25
    assert second.looks_like_me_rate == 0.75
    assert first.garment_wrong_rate == second.garment_wrong_rate == 0.25
    assert first.not_like_me_rate == 0.5
    assert second.not_like_me_rate == 0.0


async def test_provider_and_model_are_separate_dimensions(db_session: AsyncSession) -> None:
    """Not just prompt version: the grouping key is the whole triple, so a
    provider swap and a model bump never land in one bucket."""
    user_id = await _seed_user(db_session)
    await _seed_verdict(
        db_session, user_id, verdict="looks_like_me", prompt_version="p", provider="provider-a"
    )
    await _seed_verdict(
        db_session, user_id, verdict="garment_wrong", prompt_version="p", provider="provider-b"
    )
    await _seed_verdict(
        db_session, user_id, verdict="garment_wrong", prompt_version="p", model_version="m2"
    )

    keys = {
        (row.provider, row.model_version, row.prompt_version)
        for row in await static_preview_fidelity_by_version(db_session)
    }
    assert keys == {
        ("provider-a", MODEL, "p"),
        ("provider-b", MODEL, "p"),
        (PROVIDER, "m2", "p"),
    }


async def test_the_denominator_is_renders_the_user_actually_saw(
    db_session: AsyncSession,
) -> None:
    """`ready_only` is the default and it is stated, not assumed — a verdict
    attached to a render that never reached `ready` would silently change the
    meaning of every rate."""
    user_id = await _seed_user(db_session)
    await _seed_verdict(db_session, user_id, verdict="looks_like_me", prompt_version="p")
    await _seed_verdict(
        db_session,
        user_id,
        verdict="garment_wrong",
        prompt_version="p",
        generation_status="rejected_by_quality_gate",
    )

    ready_only = await static_preview_fidelity_by_version(db_session)
    assert [(row.total, row.looks_like_me_rate) for row in ready_only] == [(1, 1.0)]

    widened = await static_preview_fidelity_by_version(db_session, ready_only=False)
    assert [(row.total, row.looks_like_me_rate) for row in widened] == [(2, 0.5)]


async def test_no_single_opaque_score_is_exposed(db_session: AsyncSession) -> None:
    """BE#154: "Do not create a single opaque quality score. Keep the
    dimensions inspectable." Every field is a raw count or a named per-verdict
    rate, and every rate's denominator is present alongside it."""
    user_id = await _seed_user(db_session)
    await _seed_verdict(db_session, user_id, verdict="looks_like_me", prompt_version="p")

    row = (await static_preview_fidelity_by_version(db_session))[0].as_row()

    assert set(row) == {
        "provider",
        "model_version",
        "prompt_version",
        "total",
        "looks_like_me",
        "not_like_me",
        "garment_wrong",
        "looks_like_me_rate",
        "not_like_me_rate",
        "garment_wrong_rate",
    }
    assert row["looks_like_me"] + row["not_like_me"] + row["garment_wrong"] == row["total"]
    assert not any("score" in name or "quality" in name for name in row)


#: The complete result table this query must produce for the seed above —
#: `.agent-artifacts/v3-024/metric-query-result.md` quotes it verbatim.
#:
#: Written out in full rather than spot-checked because it IS the feature's
#: `per-version metric query result` evidence item: a green check shows no
#: numbers (CI runs `pytest -q`, which swallows stdout on a pass), so the
#: numbers have to live somewhere CI actually verifies. Here, every cell is
#: under assertion — the table is either exactly this or the check is red.
EXPECTED_ROWS = [
    {
        "provider": PROVIDER,
        "model_version": MODEL,
        "prompt_version": "p0-static-1",
        "total": 4,
        "looks_like_me": 1,
        "not_like_me": 2,
        "garment_wrong": 1,
        "looks_like_me_rate": 0.25,
        "not_like_me_rate": 0.5,
        "garment_wrong_rate": 0.25,
    },
    {
        "provider": PROVIDER,
        "model_version": MODEL,
        "prompt_version": "p0-static-2",
        "total": 4,
        "looks_like_me": 3,
        "not_like_me": 0,
        "garment_wrong": 1,
        "looks_like_me_rate": 0.75,
        "not_like_me_rate": 0.0,
        "garment_wrong_rate": 0.25,
    },
]


async def test_the_whole_result_table_is_pinned(db_session: AsyncSession) -> None:
    """The `per-version metric query result` evidence item, cell by cell.

    Also prints it, which only shows up under `-s` — the assertion is the
    part that holds in CI.
    """
    user_id = await _seed_user(db_session)
    await _seed_two_prompt_versions(db_session, user_id)

    rows = [row.as_row() for row in await static_preview_fidelity_by_version(db_session)]

    print("\n=== V3-024 per-version fidelity (static_preview_fidelity_by_version) ===")
    for row in rows:
        print(json.dumps(row, sort_keys=True))
    print(f"=== rows={len(rows)} ===")

    # Ordered, not just set-equal: `order_by` on the provenance triple is what
    # makes the table stable enough to quote as evidence.
    assert rows == EXPECTED_ROWS


def test_a_version_with_no_verdicts_yet_rates_zero_rather_than_raising() -> None:
    """Pure edge, no database: an empty triple never reaches the report through
    the query (no group is produced), so this pins the arithmetic itself."""
    empty = VersionFidelity(
        provider=PROVIDER,
        model_version=MODEL,
        prompt_version="p",
        total=0,
        looks_like_me=0,
        not_like_me=0,
        garment_wrong=0,
    )
    assert empty.looks_like_me_rate == 0.0
    assert empty.not_like_me_rate == 0.0
    assert empty.garment_wrong_rate == 0.0
