"""V3-024 — `POST /v1/outfit-visual-assets/{asset_id}/feedback` (contract §6.1).

The wire half of the feature, mapped onto its acceptance criteria:

1. *The frozen POST route exists and validates the exact vocabularies* — the
   literal body V3-020 sends is accepted, and every value outside the two
   frozen enums (plus any extra field) is 422.
2. *Only the caller's own `static_tryon` asset* — a missing asset, a foreign
   asset and the caller's own outfit-board answer the same 404, and the route
   503s with the flag off while the ungated M10 GET keeps working.
3. *Idempotent, one documented conflict rule* — an identical resubmission
   writes nothing at all (same id, same `updated_at`), a conflicting one
   replaces; either way exactly one row exists per render.
4. *Persistence carries no image / URL / prompt / provenance material* — the
   stored row's columns and the response body are both checked against the
   asset's real provenance and object key.
7. *FE PR #149's happy path no longer 404s* —
   `test_the_frozen_v3_020_body_is_recorded` posts that client's exact wire
   body (`{"verdict": "looks_like_me"}`, `aspects` **absent**, not empty).

Criteria 5 (lifecycle removal) and 6 (per-version metrics) are not wire
behavior and live in `tests/services/test_v3_preview_feedback_lifecycle.py`
and `tests/services/test_v3_preview_feedback_metrics.py`.

App-building and flag-monkeypatch conventions copied from
tests/api/test_v3_visual_preview_endpoint.py: the router reads `get_settings()`
directly (lru-cached, so it bypasses `app.dependency_overrides`), hence the
flag is patched on the consuming module.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import VisualType
from app.config.settings import Settings
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.preview_feedback import StaticPreviewFeedback
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import outfit_visual, visual_feedback
from app.presentation.middlewares.exception_handler import setup_exception_handlers

#: `aidrobe-FE/src/api/previewFeedbackMapping.ts` sends exactly this — one key,
#: `aspects` absent rather than empty. Recorded on the wire in V3-020's
#: `.agent-artifacts/v3-020/shots/driver-log.txt` scenario [2].
FE_WIRE_BODY: dict[str, Any] = {"verdict": "looks_like_me"}

PROVIDER = "fake-static-tryon"
MODEL_VERSION = "compositor-1"
PROMPT_VERSION = "p0-static-1"


def _build_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def _db() -> AsyncSession:
        return session

    app.dependency_overrides[get_db_session] = _db
    app.include_router(visual_feedback.router)
    # The ungated M10 sibling on the same prefix — contract §2 gates the new
    # surfaces only, and the pair is what proves the split router did not
    # accidentally gate V2's polling.
    app.include_router(outfit_visual.router)
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _set_flag(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    monkeypatch.setattr(
        visual_feedback,
        "get_settings",
        lambda: Settings(
            APP_ENV="dev",
            ENABLE_V3_STATIC_TRYON=enabled,
            ENABLE_V3_AVATAR=enabled,
        ),
    )


async def _seed_asset(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    visual_type: str = VisualType.STATIC_TRYON.value,
    generation_status: str = "ready",
    create_user: bool = True,
) -> uuid.UUID:
    if create_user:
        session.add(User(id=user_id))
        await session.flush()
    is_tryon = visual_type == VisualType.STATIC_TRYON.value
    asset = OutfitVisualAsset(
        user_id=user_id,
        visual_type=visual_type,
        generation_status=generation_status,
        object_key=f"users/{user_id}/tryon/static/{uuid.uuid4()}.png",
        image_url="https://storage.test/signed?token=secret",
        # The `tryon_records_provenance` CHECK requires the triple on try-on
        # rows; the M10 shapes carry none, which is also what makes them a real
        # negative case for the visual-type guard.
        provider=PROVIDER if is_tryon else None,
        model_version=MODEL_VERSION if is_tryon else None,
        prompt_version=PROMPT_VERSION if is_tryon else None,
    )
    session.add(asset)
    await session.flush()
    await session.commit()
    return asset.id


async def _post_feedback(
    client: AsyncClient,
    asset_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    body: dict[str, Any] | None = None,
) -> Any:
    return await client.post(
        f"/v1/outfit-visual-assets/{asset_id}/feedback",
        json=body if body is not None else FE_WIRE_BODY,
        headers={"X-User-Id": str(user_id)},
    )


async def _rows(session: AsyncSession) -> list[StaticPreviewFeedback]:
    # `expire_all` first, always: the request and the assertion share one
    # session, so a row already in the identity map would be handed back with
    # its pre-request values — and an idempotency test that reads a stale
    # `updated_at` passes whether or not the update was actually suppressed.
    session.expire_all()
    return list((await session.scalars(select(StaticPreviewFeedback))).all())


# --- the frozen contract ------------------------------------------------------


async def test_the_frozen_v3_020_body_is_recorded(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 1 + 7: FE #149's exact wire body answers 200, not 404."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    asset_id = await _seed_asset(db_session, user_id)
    app = _build_app(db_session)

    async with _client(app) as client:
        resp = await _post_feedback(client, asset_id, user_id)

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert set(payload) == {"asset_id", "verdict", "aspects", "recorded_at"}
    assert payload["asset_id"] == str(asset_id)
    assert payload["verdict"] == "looks_like_me"
    # `aspects` absent on the way in stays null on the way out — "did not
    # elaborate" is not "chose nothing".
    assert payload["aspects"] is None

    row = (await _rows(db_session))[0]
    assert row.verdict == "looks_like_me"
    assert row.aspects is None
    assert row.user_id == user_id
    assert row.asset_id == asset_id


async def test_every_frozen_verdict_is_accepted(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The vocabulary is three values and all three reach the database."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    app = _build_app(db_session)
    stored: list[str] = []

    for index, verdict in enumerate(("looks_like_me", "not_like_me", "garment_wrong")):
        asset_id = await _seed_asset(db_session, user_id, create_user=index == 0)
        async with _client(app) as client:
            resp = await _post_feedback(client, asset_id, user_id, body={"verdict": verdict})
        assert resp.status_code == 200, resp.text
        stored.append(resp.json()["verdict"])

    assert stored == ["looks_like_me", "not_like_me", "garment_wrong"]
    assert len(await _rows(db_session)) == 3


async def test_aspects_are_accepted_and_normalized(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sorted and deduplicated, so re-tap order cannot look like a new answer."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    asset_id = await _seed_asset(db_session, user_id)
    app = _build_app(db_session)

    async with _client(app) as client:
        resp = await _post_feedback(
            client,
            asset_id,
            user_id,
            body={"verdict": "not_like_me", "aspects": ["face", "body_ratio", "face"]},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["aspects"] == ["body_ratio", "face"]
    assert (await _rows(db_session))[0].aspects == ["body_ratio", "face"]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"verdict": "looks_ok"}, id="verdict-outside-the-vocabulary"),
        pytest.param({"verdict": "looks_like_me", "aspects": ["vibes"]}, id="aspect-outside"),
        pytest.param({}, id="verdict-missing"),
        pytest.param(
            {"verdict": "looks_like_me", "comment": "too baggy"}, id="free-text-smuggled"
        ),
        pytest.param(
            {"verdict": "looks_like_me", "score": 0.4}, id="score-smuggled"
        ),
    ],
)
async def test_out_of_contract_bodies_are_422(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    """Acceptance 1. `extra="forbid"` matters most here: a comment field is the
    tempting addition, and v3-analytics-events §5 rules free text out."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    asset_id = await _seed_asset(db_session, user_id)
    app = _build_app(db_session)

    async with _client(app) as client:
        resp = await _post_feedback(client, asset_id, user_id, body=body)

    assert resp.status_code == 422, resp.text
    assert await _rows(db_session) == []


# --- ownership, existence and type (acceptance 2) -----------------------------


async def test_a_foreign_asset_is_404_not_403(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §3: 403 would confirm the id exists."""
    _set_flag(monkeypatch, enabled=True)
    owner_id = uuid.uuid4()
    stranger_id = uuid.uuid4()
    asset_id = await _seed_asset(db_session, owner_id)
    db_session.add(User(id=stranger_id))
    await db_session.commit()
    app = _build_app(db_session)

    async with _client(app) as client:
        foreign = await _post_feedback(client, asset_id, stranger_id)
        missing = await _post_feedback(client, uuid.uuid4(), stranger_id)

    # Byte-identical answers: nothing distinguishes "exists but is not yours"
    # from "does not exist".
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert foreign.json()["code"] == 4041
    assert await _rows(db_session) == []


async def test_a_missing_asset_is_404(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    await _seed_asset(db_session, user_id)
    app = _build_app(db_session)

    async with _client(app) as client:
        resp = await _post_feedback(client, uuid.uuid4(), user_id)

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == 4041


async def test_the_callers_own_outfit_board_is_404(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BE#154: this must not become a generic image-feedback endpoint. The
    three verdicts are meaningless for a flat-lay board, and answering anything
    other than "no feedback surface here" is the first step of that drift."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    board_id = await _seed_asset(
        db_session, user_id, visual_type=VisualType.OUTFIT_BOARD.value
    )
    app = _build_app(db_session)

    async with _client(app) as client:
        resp = await _post_feedback(client, board_id, user_id)

    assert resp.status_code == 404, resp.text
    assert await _rows(db_session) == []


# --- the flag (contract §2) ---------------------------------------------------


async def test_flag_off_is_503_before_the_body_is_read(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A router-level gate, so a malformed body still 503s rather than 422ing —
    a 422 would leak the request schema of a surface that does not exist yet."""
    _set_flag(monkeypatch, enabled=False)
    user_id = uuid.uuid4()
    asset_id = await _seed_asset(db_session, user_id)
    app = _build_app(db_session)

    async with _client(app) as client:
        valid = await _post_feedback(client, asset_id, user_id)
        malformed = await _post_feedback(client, asset_id, user_id, body={"verdict": "nope"})

    assert valid.status_code == 503, valid.text
    assert malformed.status_code == 503, malformed.text
    assert await _rows(db_session) == []


async def test_the_m10_get_on_the_same_prefix_stays_ungated(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The risk this feature's second router introduces: contract §2 gates the
    NEW surfaces only, and V2's outfit-board polling depends on that GET."""
    _set_flag(monkeypatch, enabled=False)
    user_id = uuid.uuid4()
    asset_id = await _seed_asset(
        db_session, user_id, visual_type=VisualType.OUTFIT_BOARD.value
    )
    app = _build_app(db_session)

    async with _client(app) as client:
        resp = await client.get(
            f"/v1/outfit-visual-assets/{asset_id}", headers={"X-User-Id": str(user_id)}
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["asset_id"] == str(asset_id)


# --- idempotency and the conflict rule (acceptance 3) -------------------------


async def test_an_identical_resubmission_writes_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored row is seeded with a past `updated_at` so "no-op" is
    observable: a suppressed `DO UPDATE` leaves that timestamp alone, while an
    unconditional one would move it to the transaction clock."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    asset_id = await _seed_asset(db_session, user_id)
    long_ago = datetime.now(UTC) - timedelta(days=3)
    seeded = StaticPreviewFeedback(
        user_id=user_id,
        asset_id=asset_id,
        verdict="looks_like_me",
        aspects=None,
        created_at=long_ago,
        updated_at=long_ago,
    )
    db_session.add(seeded)
    await db_session.commit()
    original_id = seeded.id
    app = _build_app(db_session)

    async with _client(app) as client:
        resp = await _post_feedback(client, asset_id, user_id)

    assert resp.status_code == 200, resp.text
    assert resp.json()["verdict"] == "looks_like_me"
    assert datetime.fromisoformat(resp.json()["recorded_at"]) == long_ago

    rows = await _rows(db_session)
    assert len(rows) == 1
    assert rows[0].id == original_id
    assert rows[0].updated_at == long_ago


async def test_a_double_tap_does_not_double_count(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client-discipline-free version: two identical POSTs, one row."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    asset_id = await _seed_asset(db_session, user_id)
    app = _build_app(db_session)

    async with _client(app) as client:
        first = await _post_feedback(client, asset_id, user_id)
        second = await _post_feedback(client, asset_id, user_id)

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json() == second.json()
    assert len(await _rows(db_session)) == 1


async def test_a_conflicting_resubmission_replaces_the_verdict(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one documented rule: last write wins, and the row count stays 1 —
    a change of mind is one current answer, not two inflating both rates."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    asset_id = await _seed_asset(db_session, user_id)
    long_ago = datetime.now(UTC) - timedelta(days=3)
    db_session.add(
        StaticPreviewFeedback(
            user_id=user_id,
            asset_id=asset_id,
            verdict="looks_like_me",
            aspects=None,
            created_at=long_ago,
            updated_at=long_ago,
        )
    )
    await db_session.commit()
    app = _build_app(db_session)

    async with _client(app) as client:
        resp = await _post_feedback(
            client,
            asset_id,
            user_id,
            body={"verdict": "garment_wrong", "aspects": ["garment_fit"]},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["verdict"] == "garment_wrong"

    rows = await _rows(db_session)
    assert len(rows) == 1
    assert rows[0].verdict == "garment_wrong"
    assert rows[0].aspects == ["garment_fit"]
    assert rows[0].updated_at > long_ago
    # The row is amended, not replaced: `created_at` still says when the user
    # first answered.
    assert rows[0].created_at == long_ago


async def test_two_users_two_assets_stay_independent(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotency is per render, not per user or global."""
    _set_flag(monkeypatch, enabled=True)
    first_user = uuid.uuid4()
    second_user = uuid.uuid4()
    first_asset = await _seed_asset(db_session, first_user)
    second_asset = await _seed_asset(db_session, second_user)
    app = _build_app(db_session)

    async with _client(app) as client:
        a = await _post_feedback(client, first_asset, first_user)
        b = await _post_feedback(
            client, second_asset, second_user, body={"verdict": "garment_wrong"}
        )

    assert (a.status_code, b.status_code) == (200, 200)
    rows = {row.asset_id: row.verdict for row in await _rows(db_session)}
    assert rows == {first_asset: "looks_like_me", second_asset: "garment_wrong"}


# --- what must never be persisted or returned (acceptance 4) ------------------


async def test_neither_the_row_nor_the_response_carries_render_material(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No image bytes, no signed URL, no object key, no prompt text, and no
    provider / model / prompt version — §6.4 keeps provenance internal, and the
    feedback record joins to it rather than copying it."""
    _set_flag(monkeypatch, enabled=True)
    user_id = uuid.uuid4()
    asset_id = await _seed_asset(db_session, user_id)
    app = _build_app(db_session)

    async with _client(app) as client:
        resp = await _post_feedback(client, asset_id, user_id)

    assert resp.status_code == 200, resp.text
    body = resp.text
    asset = await db_session.get(OutfitVisualAsset, asset_id)
    assert asset is not None
    for secret in (
        PROVIDER,
        MODEL_VERSION,
        PROMPT_VERSION,
        asset.object_key or "",
        asset.image_url or "",
    ):
        assert secret not in body

    columns = {column.name for column in StaticPreviewFeedback.__table__.columns}
    assert columns == {
        "id",
        "user_id",
        "asset_id",
        "verdict",
        "aspects",
        "created_at",
        "updated_at",
    }
