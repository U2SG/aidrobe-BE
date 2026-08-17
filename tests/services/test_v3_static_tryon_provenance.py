"""V3-016 acceptance 3 — provenance on assets, against a real Postgres.

What only a database can prove, and is therefore here rather than in a unit test:

- a `static_tryon` row that does not name its pipeline is **refused**, so
  "provider/model/prompt version are recorded" is a constraint rather than a
  convention someone has to remember in V3-017;
- the constraint is scoped — M10 rows without provenance still insert, so the
  existing OOTD/board write paths are untouched;
- the three columns round-trip the exact identity the port reported, taken from
  a real `render` rather than from hand-written strings;
- and the endpoint that serves these rows still returns the frozen M10 shape,
  with none of the three columns on it (acceptance 4 at the API level).

**Declared limit, stated where it can be seen.** V3-016 adds no router and
enqueues nothing, so the row here is hand-seeded; the constraint's real write
path arrives with V3-017. That is the same shape of limit V3-003 recorded for
its flag-gating criterion, and the migration's docstring says why imposing it
from here is safe: the pipeline identity is a pure function of the adapter
`container.static_tryon()` selects, so it is known at enqueue time — contract
§6.3 already requires it there, as a component of the strong cache key.
"""

from __future__ import annotations

import io
import uuid

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.static_tryon import (
    GarmentImage,
    GarmentSlot,
    PoseTemplate,
    StaticTryOnRequest,
    ViewRequirement,
)
from app.bootstrap.container import static_tryon
from app.common.enums import VisualType
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import outfit_visual as visual_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def _image(size: tuple[int, int], colour: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _request() -> StaticTryOnRequest:
    """Synthetic material only — no real photograph is read anywhere here."""
    return StaticTryOnRequest(
        avatar_image=_image((768, 1024), (170, 150, 140)),
        candidate_garment=GarmentImage(
            image=_image((400, 400), (20, 90, 160)), slot=GarmentSlot.TORSO
        ),
        supporting_garments=(
            GarmentImage(image=_image((400, 400), (40, 40, 60)), slot=GarmentSlot.LEGS),
        ),
        pose_template=PoseTemplate.NEUTRAL_FULL_BODY,
        view=ViewRequirement.FRONT,
    )


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    return user_id


def _build_visual_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return Settings(APP_ENV="dev")

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(visual_router.router)
    return app


async def test_a_rendered_preview_records_the_pipeline_that_produced_it(
    db_session: AsyncSession,
) -> None:
    """The whole acceptance-3 chain in one test, with no string invented by hand.

    Container-selected adapter → real `render` → the result's identity written to
    the row → read back out of Postgres. If any link stamped a different value
    the equality at the end fails.
    """
    user_id = await _seed_user(db_session)
    port = static_tryon()

    result = await port.render(_request())

    asset = OutfitVisualAsset(
        user_id=user_id,
        visual_type=VisualType.STATIC_TRYON.value,
        generation_status="ready",
        object_key=f"users/{user_id}/tryon/{uuid.uuid4()}.png",
        provider=result.identity.provider,
        model_version=result.identity.model_version,
        prompt_version=result.identity.prompt_version,
    )
    db_session.add(asset)
    await db_session.flush()
    # Read the id out *before* expiring: touching an expired attribute triggers a
    # synchronous lazy refresh, which is illegal IO under asyncio (the same trap
    # V3-003 hit with `expire_all` and `user.id`).
    asset_id = asset.id
    db_session.expire(asset)

    stored = await db_session.get(OutfitVisualAsset, asset_id)
    assert stored is not None
    assert (stored.provider, stored.model_version, stored.prompt_version) == (
        port.pipeline_identity.provider,
        port.pipeline_identity.model_version,
        port.pipeline_identity.prompt_version,
    )
    # And the row holds a stable object key, never a signed URL — the V3-011
    # rule, restated here because this is the first V3 row on this table.
    assert stored.image_url is None
    assert stored.object_key is not None and "http" not in stored.object_key


@pytest.mark.parametrize(
    "missing",
    ["provider", "model_version", "prompt_version"],
)
async def test_a_try_on_row_without_its_pipeline_is_refused(
    db_session: AsyncSession, missing: str
) -> None:
    """Each column independently, so a partial stamp cannot slip through.

    A single "all three missing" case would pass even if the constraint only
    checked `provider`.
    """
    user_id = await _seed_user(db_session)
    provenance = {
        "provider": "fake",
        "model_version": "fake-static-tryon-1",
        "prompt_version": "v1",
    }
    provenance[missing] = None

    db_session.add(
        OutfitVisualAsset(
            user_id=user_id,
            visual_type=VisualType.STATIC_TRYON.value,
            generation_status="queued",
            **provenance,
        )
    )

    with pytest.raises(IntegrityError) as excinfo:
        await db_session.flush()
    assert "tryon_records_provenance" in str(excinfo.value)


async def test_the_existing_m10_write_paths_are_untouched(db_session: AsyncSession) -> None:
    """The constraint is conditional, and this is what that buys.

    `ootd_poster` and `outfit_board` rows predate provenance. If the constraint
    were a blanket NOT NULL, every existing generation path would have to invent
    a `provider` for a render no provider produced — so it is scoped to
    `static_tryon` and this asserts the scope rather than trusting the SQL.

    The `None` case is deliberate and is the one that needs a database to
    settle: `visual_type` is nullable, and `NULL <> 'static_tryon'` evaluates to
    NULL, not TRUE — a CHECK passes on NULL because NULL is not FALSE. So a
    typeless row inserts, which is correct (it is not a try-on row) but is a
    three-valued-logic detail no reading of the SQL should be trusted on.
    """
    user_id = await _seed_user(db_session)

    for visual_type in (VisualType.OOTD_POSTER.value, VisualType.OUTFIT_BOARD.value, None):
        db_session.add(
            OutfitVisualAsset(
                user_id=user_id, visual_type=visual_type, generation_status="queued"
            )
        )
    await db_session.flush()

    count = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(OutfitVisualAsset)
        .where(OutfitVisualAsset.user_id == user_id)
    )
    assert count == 3


async def test_the_public_asset_endpoint_does_not_serve_the_pipeline(
    db_session: AsyncSession,
) -> None:
    """Acceptance 4, end to end over a row that actually carries provenance.

    The unit-level leakage tests read the response model and the OpenAPI
    document; this one reads the bytes an HTTP client receives for a
    `static_tryon` asset whose three columns are populated — the case where a
    leak would actually have something to leak.
    """
    user_id = await _seed_user(db_session)
    identity = static_tryon().pipeline_identity
    asset = OutfitVisualAsset(
        user_id=user_id,
        visual_type=VisualType.STATIC_TRYON.value,
        generation_status="ready",
        object_key=f"users/{user_id}/tryon/{uuid.uuid4()}.png",
        provider=identity.provider,
        model_version=identity.model_version,
        prompt_version=identity.prompt_version,
    )
    db_session.add(asset)
    await db_session.flush()

    app = _build_visual_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/v1/outfit-visual-assets/{asset.id}", headers={"X-User-Id": str(user_id)}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["visual_type"] == VisualType.STATIC_TRYON.value
    assert set(body) == {
        "asset_id",
        "generation_status",
        "image_url",
        "visual_type",
        "error_code",
        "error_message",
        # V3-019 — contract §6.2's ADDITIVE nullable pair. Growing the frozen
        # set is a contract amendment, not a weakening: the leak claim below
        # is carried by the substring check, not by key count.
        "created_at",
        "expires_at",
    }
    # Not just "the keys are absent" — the value must not appear anywhere in the
    # serialized payload either, so a nested or renamed field cannot carry it
    # through. Only `model_version` is checked by substring: it is the one
    # component distinctive enough to be unambiguous, whereas `provider`
    # ("fake") and `prompt_version` ("v1") are short enough to collide with a
    # storage host or the `/v1/` path prefix and would make this assertion say
    # less than it appears to. Those two are covered by the exact key set above.
    assert identity.model_version not in response.text
