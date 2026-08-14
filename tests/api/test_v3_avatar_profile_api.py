"""V3-013 — the six frozen avatar surfaces over HTTP (contract §4.2).

What only the API level can show: the flag really returns 503 on every surface,
the frozen response shape is what a client receives, `purge_derived` really is
required, and a client-supplied storage reference cannot reach another user's
object.

The state transitions themselves are asserted in
`tests/services/test_v3_avatar_lifecycle_apis.py`; this file is about the wire.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.avatar import enqueue_candidate_detection as enqueue_module
from app.application.avatar.delete_avatar import JOB_TYPE as PURGE_JOB_TYPE
from app.application.avatar.prepare_avatar import JOB_TYPE as PREPARATION_JOB_TYPE
from app.common.enums import (
    AvatarConsentStatus,
    AvatarQualityStatus,
    AvatarSourceType,
    AvatarStatus,
)
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.avatar import (
    AvatarProfile,
    AvatarSourceAsset,
    AvatarVersion,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.avatar_repo import AvatarLifecycle
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import avatar as avatar_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers

CONSENT_COPY_V1 = "avatar_consent_placeholder_v1"
STORAGE_BASE = Settings().LOCAL_STORAGE_BASE_URL.rstrip("/")
FOREIGN_USER = uuid.UUID("99999999-2222-3333-4444-555555555555")


def _build_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return Settings(APP_ENV="dev", ENABLE_V3_AVATAR=True)

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(avatar_router.router)
    return app


def _enable(monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True) -> None:
    """The flag must be patched on the *consuming module*.

    `get_settings` is `lru_cache`d, so a FastAPI dependency override does not
    reach the router's direct call (BE CLAUDE.md test-infrastructure note).
    """

    def _settings() -> Settings:
        return Settings(APP_ENV="dev", ENABLE_V3_AVATAR=enabled)

    monkeypatch.setattr(avatar_router, "get_settings", _settings)


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    return user_id


def _headers(user_id: uuid.UUID) -> dict[str, str]:
    return {"X-User-Id": str(user_id)}


async def _seed_candidate(
    session: AsyncSession,
    *,
    source_type: AvatarSourceType = AvatarSourceType.OOTD_PHOTO,
) -> tuple[uuid.UUID, AvatarProfile, AvatarSourceAsset]:
    user_id = await _seed_user(session)
    profile = AvatarProfile(user_id=user_id, status=AvatarStatus.NONE.value)
    session.add(profile)
    asset = AvatarSourceAsset(
        user_id=user_id,
        source_type=source_type.value,
        source_object_key=f"users/{user_id}/ootd/selfie.jpg",
        quality_status=AvatarQualityStatus.PASSED.value,
        consent_status=AvatarConsentStatus.PENDING.value,
    )
    session.add(asset)
    await session.flush()
    await AvatarLifecycle(session).offer_candidate(profile, asset)
    return user_id, profile, asset


# --- the flag gate ---------------------------------------------------------


async def test_every_surface_is_503_when_the_flag_is_off(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §2 rollback rule 1, on all six surfaces rather than a sample.

    A gate that covers five of six is the one that matters — so each is listed.
    503 with `4003 FORBIDDEN` is the frozen shape (the `ENABLE_SENSE_CHECK`
    precedent), and FE hides a V3 entry silently on it.
    """
    _enable(monkeypatch, enabled=False)
    user_id = await _seed_user(db_session)
    headers = _headers(user_id)
    asset_id = uuid.uuid4()

    responses = [
        await client.get("/v1/avatar-profile", headers=headers),
        await client.post(
            f"/v1/avatar-candidates/{asset_id}/consent",
            json={"accept": False},
            headers=headers,
        ),
        await client.post(
            "/v1/avatar-profile/uploads",
            json={"object_key": f"users/{user_id}/avatar/a.jpg"},
            headers=headers,
        ),
        await client.post("/v1/avatar-profile/prepare", headers=headers),
        await client.post("/v1/avatar-profile/replace", headers=headers),
        await client.request(
            "DELETE", "/v1/avatar-profile?purge_derived=true", headers=headers
        ),
    ]

    assert [r.status_code for r in responses] == [503] * 6
    for response in responses:
        assert response.json()["code"] == 4003


async def test_rows_survive_the_flag_being_turned_off(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §2 rollback rule 3: the flag gates exposure, not persistence.

    Turning it back on restores function with no migration — so a consented
    avatar is still consented after a rollback, rather than a user having to agree
    a second time.
    """
    _enable(monkeypatch)
    user_id, profile, asset = await _seed_candidate(db_session)
    accepted = await client.post(
        f"/v1/avatar-candidates/{asset.id}/consent",
        json={"accept": True, "consent_version": CONSENT_COPY_V1},
        headers=_headers(user_id),
    )
    assert accepted.status_code == 200

    _enable(monkeypatch, enabled=False)
    hidden = await client.get("/v1/avatar-profile", headers=_headers(user_id))
    _enable(monkeypatch)
    visible = await client.get("/v1/avatar-profile", headers=_headers(user_id))

    assert hidden.status_code == 503
    assert visible.json()["consent"]["version"] == CONSENT_COPY_V1


# --- GET /v1/avatar-profile ------------------------------------------------


async def test_a_user_with_no_avatar_gets_status_none_and_a_200(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"`status=none` is a normal 200, not 404" (contract §4.2), verbatim.

    A 404 would make "no avatar yet" indistinguishable from an error for FE's
    optional, self-hiding entry.
    """
    _enable(monkeypatch)
    user_id = await _seed_user(db_session)

    response = await client.get("/v1/avatar-profile", headers=_headers(user_id))

    assert response.status_code == 200
    assert response.json() == {
        "status": "none",
        "active_version": None,
        "candidate": None,
        "consent": None,
    }


async def test_a_candidate_profile_returns_the_frozen_candidate_block(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `candidate` block, with a signed preview URL and no stored URL.

    `preview_url` is signed at read time from the stable object key — V3_SPEC
    §13.5 forbids persisting the TTL-bound value, and V3-011 has no column that
    could hold one.
    """
    _enable(monkeypatch)
    user_id, profile, asset = await _seed_candidate(db_session)

    body = (await client.get("/v1/avatar-profile", headers=_headers(user_id))).json()

    assert body["status"] == "candidate"
    assert body["candidate"]["source_asset_id"] == str(asset.id)
    assert body["candidate"]["source_type"] == AvatarSourceType.OOTD_PHOTO.value
    assert body["candidate"]["preview_url"]
    assert body["active_version"] is None
    assert body["consent"] is None


async def test_a_ready_profile_returns_the_active_version_and_no_candidate(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    user_id, profile, asset = await _seed_candidate(db_session)
    lifecycle = AvatarLifecycle(db_session)
    await lifecycle.record_consent(
        profile, asset, accept=True, consent_version=CONSENT_COPY_V1
    )
    version = AvatarVersion(
        avatar_profile_id=profile.id,
        source_asset_ids=[str(asset.id)],
        provider="fake",
        model_version="m1",
        prompt_version="p1",
        canonical_front_object_key=f"users/{user_id}/avatar/canonical/front.png",
    )
    db_session.add(version)
    await db_session.flush()
    await lifecycle.activate_version(profile, version)

    body = (await client.get("/v1/avatar-profile", headers=_headers(user_id))).json()

    assert body["status"] == "ready"
    assert body["active_version"]["id"] == str(version.id)
    assert body["active_version"]["canonical_front_url"]
    assert body["candidate"] is None
    assert body["consent"]["version"] == CONSENT_COPY_V1


async def test_the_response_carries_no_provider_internals(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §6.4: provider / model / prompt versions never reach FE."""
    _enable(monkeypatch)
    user_id, profile, asset = await _seed_candidate(db_session)

    raw = (await client.get("/v1/avatar-profile", headers=_headers(user_id))).text

    for internal in ("provider", "model_version", "prompt_version"):
        assert internal not in raw


# --- POST …/consent -------------------------------------------------------


async def test_accepting_moves_the_profile_to_preparing(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    user_id, profile, asset = await _seed_candidate(db_session)

    response = await client.post(
        f"/v1/avatar-candidates/{asset.id}/consent",
        json={"accept": True, "consent_version": CONSENT_COPY_V1},
        headers=_headers(user_id),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == AvatarStatus.PREPARING.value
    assert body["consent_status"] == AvatarConsentStatus.ACCEPTED.value
    assert body["consent_version"] == CONSENT_COPY_V1
    assert body["consented_at"] is not None


async def test_accepting_without_a_consent_version_is_rejected(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"`accept=true` requires the consent copy version" (contract §4.2).

    Acceptance 1 depends on it: a consent with no version is an audit record that
    cannot say what the user was shown.
    """
    _enable(monkeypatch)
    user_id, profile, asset = await _seed_candidate(db_session)

    response = await client.post(
        f"/v1/avatar-candidates/{asset.id}/consent",
        json={"accept": True},
        headers=_headers(user_id),
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4001
    assert asset.consent_status == AvatarConsentStatus.PENDING.value


async def test_declining_with_a_consent_version_is_rejected(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A "declined under version X" record would imply agreement that never was."""
    _enable(monkeypatch)
    user_id, profile, asset = await _seed_candidate(db_session)

    response = await client.post(
        f"/v1/avatar-candidates/{asset.id}/consent",
        json={"accept": False, "consent_version": CONSENT_COPY_V1},
        headers=_headers(user_id),
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4001


async def test_consenting_to_another_users_candidate_is_not_found(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """404, not 403 — the response must not confirm the asset exists (§3)."""
    _enable(monkeypatch)
    _victim, _victim_profile, victim_asset = await _seed_candidate(db_session)
    attacker_id = await _seed_user(db_session)

    response = await client.post(
        f"/v1/avatar-candidates/{victim_asset.id}/consent",
        json={"accept": True, "consent_version": CONSENT_COPY_V1},
        headers=_headers(attacker_id),
    )

    assert response.status_code == 404
    assert victim_asset.consent_status == AvatarConsentStatus.PENDING.value
    assert victim_asset.consent_version is None


async def test_re_deciding_a_decided_candidate_is_a_conflict(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mapped to the frozen `4091 STATE_CONFLICT`, not a 500."""
    _enable(monkeypatch)
    user_id, profile, asset = await _seed_candidate(db_session)
    await client.post(
        f"/v1/avatar-candidates/{asset.id}/consent",
        json={"accept": False},
        headers=_headers(user_id),
    )

    response = await client.post(
        f"/v1/avatar-candidates/{asset.id}/consent",
        json={"accept": True, "consent_version": CONSENT_COPY_V1},
        headers=_headers(user_id),
    )

    assert response.status_code == 409
    assert response.json()["code"] == 4091


# --- POST /v1/avatar-profile/uploads (source C) ----------------------------


async def test_consent_emits_the_contracted_analytics_event_once(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review on BE#147, P1 — the frozen `v3_avatar_consent` emitter.

    The analytics contract §0 assigns it to this feature by name ("emitters ship
    with the feature that owns the surface ... V3-013 avatar"). Without it every
    consent is missing from the consent-rate data the kill criteria are read
    from, and no later feature is assigned to add it — V3-014/V3-015 are FE.

    Emitted once per real decision: `record_consent` treats an exact repeat as an
    idempotent retry, and counting a network retry as a second consent would
    inflate the rate with duplicates of one user action.
    """
    _enable(monkeypatch)
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(
        avatar_router,
        "emit_v3_event",
        lambda name, fields, **kw: emitted.append({"name": name, "fields": dict(fields), **kw}),
    )
    user_id, profile, asset = await _seed_candidate(db_session)

    body = {"accept": True, "consent_version": CONSENT_COPY_V1}
    first = await client.post(
        f"/v1/avatar-candidates/{asset.id}/consent", json=body, headers=_headers(user_id)
    )
    repeat = await client.post(
        f"/v1/avatar-candidates/{asset.id}/consent", json=body, headers=_headers(user_id)
    )

    assert first.status_code == 200
    assert repeat.status_code == 200, "an exact repeat is an idempotent retry, not a conflict"
    assert len(emitted) == 1, "the idempotent repeat must not be counted twice"
    assert emitted[0]["name"] == "v3_avatar_consent"
    # Exactly the frozen field set: no photo, no key, no consent copy text.
    assert emitted[0]["fields"] == {
        "accepted": True,
        "source_type": AvatarSourceType.OOTD_PHOTO.value,
    }
    assert emitted[0]["user_id"] == user_id


async def test_a_declined_consent_is_also_emitted(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`accepted` is a field of the event, not a filter on it — a consent rate
    computed from acceptances alone has no denominator."""
    _enable(monkeypatch)
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(
        avatar_router,
        "emit_v3_event",
        lambda name, fields, **kw: emitted.append({"name": name, "fields": dict(fields), **kw}),
    )
    user_id, profile, asset = await _seed_candidate(db_session)

    await client.post(
        f"/v1/avatar-candidates/{asset.id}/consent",
        json={"accept": False},
        headers=_headers(user_id),
    )

    assert len(emitted) == 1
    assert emitted[0]["fields"]["accepted"] is False


async def test_deletion_emits_the_contracted_analytics_event(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review on BE#147, P2 — the frozen `v3_avatar_deleted` emitter.

    `not_applicable` for `flow_id`, so none may be passed: `validate_v3_event`
    rejects the event outright if one is, and `emit_v3_event` swallows the
    failure with a warning. Getting that backwards deletes the event, not the
    request, which is why the absence of a flow is asserted rather than assumed.
    """
    _enable(monkeypatch)
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(
        avatar_router,
        "emit_v3_event",
        lambda name, fields, **kw: emitted.append({"name": name, "fields": dict(fields), **kw}),
    )
    user_id, profile, asset = await _seed_candidate(db_session)

    response = await client.delete(
        "/v1/avatar-profile?purge_derived=true", headers=_headers(user_id)
    )

    assert response.status_code == 200
    assert len(emitted) == 1
    assert emitted[0]["name"] == "v3_avatar_deleted"
    assert emitted[0]["fields"] == {"purge_derived": True}
    assert emitted[0].get("flow_id") is None


async def test_registering_an_upload_queues_detection_as_a_preview_request(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Source C hands the photo to V3-012's `PREVIEW_REQUEST` trigger.

    Registration is not consent: the photo is assessed and, if it qualifies,
    offered — the user still has to accept. V3-012 recorded "no caller passes
    PREVIEW_REQUEST yet" as an open risk; this is that caller.
    """
    _enable(monkeypatch)
    user_id = await _seed_user(db_session)

    response = await client.post(
        "/v1/avatar-profile/uploads",
        json={"object_key": f"users/{user_id}/avatar/deliberate.jpg"},
        headers=_headers(user_id),
    )

    assert response.status_code == 200
    assert response.json()["detection_outcome"] == "enqueued"
    job = await db_session.scalar(
        sa.select(AsyncJob).where(AsyncJob.job_type == enqueue_module.JOB_TYPE)
    )
    assert job is not None
    payload = job.payload or {}
    assert payload["trigger"] == "preview_request"
    assert payload["source_type"] == AvatarSourceType.USER_UPLOAD.value
    # The stable key, not the signed URL the router resolved on the way in.
    assert payload["object_key"] == f"users/{user_id}/avatar/deliberate.jpg"
    assert "http" not in payload["object_key"]


async def test_a_broken_url_signer_cannot_silently_drop_the_upload(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This surface must not depend on signing a key just to un-sign it.

    The first version of this endpoint passed
    `image_url=resolve_render_url(object_key, None)` and let
    `extract_internal_object_key` recover the key. That round-trip could not fail
    loudly: `resolve_render_url` swallows a signing exception and returns its
    fallback — `None` here — so a storage misconfiguration produced
    `NO_INTERNAL_OBJECT_KEY`, and an `aliyun_oss` per-purpose-bucket config
    produced `UNRESOLVABLE_STORAGE_BUCKET`. Either way the endpoint answered
    **HTTP 200** while quietly discarding a photo the user deliberately took for
    this purpose, which is V3-012's round-2 "reported an outcome the system did
    not perform" finding in a new place.

    Breaking the signer outright is the discriminating test: under the old code
    this returned 200 with no job, and it now enqueues normally, because the key
    never goes near a URL.
    """
    _enable(monkeypatch)
    user_id = await _seed_user(db_session)

    class _BrokenSigner:
        def sign_render_url(self, key: str, **kwargs: object) -> str:
            raise RuntimeError("storage signing is misconfigured")

    monkeypatch.setattr(
        "app.bootstrap.container.object_storage", lambda: _BrokenSigner()
    )

    response = await client.post(
        "/v1/avatar-profile/uploads",
        json={"object_key": f"users/{user_id}/avatar/deliberate.jpg"},
        headers=_headers(user_id),
    )

    assert response.status_code == 200
    assert response.json()["detection_outcome"] == "enqueued"
    job = await db_session.scalar(
        sa.select(AsyncJob).where(AsyncJob.job_type == enqueue_module.JOB_TYPE)
    )
    assert job is not None
    assert (job.payload or {})["object_key"] == f"users/{user_id}/avatar/deliberate.jpg"


async def test_registering_another_users_object_is_not_found(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cross-user read V3-012's round-3 review found, closed at this surface.

    `object_key` is free-form client input and the worker would read it with
    backend credentials — bypassing the access control on whatever URL the caller
    actually held. Without the ownership check, naming somebody else's key would
    have their photo assessed and persisted as this caller's avatar candidate, and
    `GET /v1/avatar-profile` would then sign it back to them as `preview_url`.
    """
    _enable(monkeypatch)
    user_id = await _seed_user(db_session)

    response = await client.post(
        "/v1/avatar-profile/uploads",
        json={"object_key": f"users/{FOREIGN_USER}/avatar/private.jpg"},
        headers=_headers(user_id),
    )

    assert response.status_code == 404
    assert await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(AsyncJob)
        .where(AsyncJob.job_type == enqueue_module.JOB_TYPE)
    ) == 0


async def test_a_traversal_key_is_refused(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key that starts with the right prefix can still address something else."""
    _enable(monkeypatch)
    user_id = await _seed_user(db_session)

    response = await client.post(
        "/v1/avatar-profile/uploads",
        json={"object_key": f"users/{user_id}/../{FOREIGN_USER}/avatar/private.jpg"},
        headers=_headers(user_id),
    )

    assert response.status_code == 404


async def test_an_upload_reference_is_required(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    user_id = await _seed_user(db_session)

    response = await client.post(
        "/v1/avatar-profile/uploads", json={}, headers=_headers(user_id)
    )

    assert response.status_code == 400
    assert response.json()["code"] == 4001


async def test_a_file_url_is_accepted_and_resolved_to_its_key(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clients hold both the key and the URL presign returned; either works."""
    _enable(monkeypatch)
    user_id = await _seed_user(db_session)
    object_key = f"users/{user_id}/avatar/from-url.jpg"

    response = await client.post(
        "/v1/avatar-profile/uploads",
        json={"file_url": f"{STORAGE_BASE}/files/{object_key}"},
        headers=_headers(user_id),
    )

    assert response.status_code == 200
    job = await db_session.scalar(
        sa.select(AsyncJob).where(AsyncJob.job_type == enqueue_module.JOB_TYPE)
    )
    assert job is not None
    assert (job.payload or {})["object_key"] == object_key


# --- prepare / replace ----------------------------------------------------


async def test_prepare_returns_the_job_and_a_second_call_conflicts(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 2 over HTTP: one job, and the frozen 409 on a duplicate."""
    _enable(monkeypatch)
    user_id, profile, asset = await _seed_candidate(db_session)
    await client.post(
        f"/v1/avatar-candidates/{asset.id}/consent",
        json={"accept": True, "consent_version": CONSENT_COPY_V1},
        headers=_headers(user_id),
    )

    first = await client.post("/v1/avatar-profile/prepare", headers=_headers(user_id))
    second = await client.post("/v1/avatar-profile/prepare", headers=_headers(user_id))

    assert first.status_code == 200
    assert first.json()["created"] is True
    assert first.json()["status"] == AvatarStatus.PREPARING.value
    assert second.status_code == 409
    assert second.json()["code"] == 4091
    assert await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(AsyncJob)
        .where(AsyncJob.job_type == PREPARATION_JOB_TYPE)
    ) == 1


async def test_a_user_whose_preparation_died_can_start_another_one(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dead end, from where the user actually stands.

    Their preparation ran out of attempts, so the profile is back to `candidate`
    with the consent still on file. Before the fix this surface answered the
    retry with `200 {"created": false}` and the id of a job that would never run
    again — a success message for work that was not happening, recoverable only
    by taking a new photograph. The worker-level test proves the retry reaches
    `ready`; this proves the client is told the truth.
    """
    _enable(monkeypatch)
    user_id, profile, asset = await _seed_candidate(db_session)
    await client.post(
        f"/v1/avatar-candidates/{asset.id}/consent",
        json={"accept": True, "consent_version": CONSENT_COPY_V1},
        headers=_headers(user_id),
    )
    first = await client.post("/v1/avatar-profile/prepare", headers=_headers(user_id))
    job_id = uuid.UUID(first.json()["job_id"])

    # What the worker leaves behind once the attempts are exhausted.
    job = await db_session.get(AsyncJob, job_id)
    assert job is not None
    job.status = "dead"
    job.attempts = job.max_attempts
    job.last_error = "RuntimeError"
    await db_session.flush()
    await AvatarLifecycle(db_session).fail_preparation(profile)

    before = await client.get("/v1/avatar-profile", headers=_headers(user_id))
    assert before.json()["status"] == AvatarStatus.CANDIDATE.value
    # Declared rather than discovered: the consented photo is no longer
    # *pending*, so the frozen `candidate` block is null even though the status
    # is `candidate`. The affordance in this state is retry (`prepare`), not a
    # second consent — V3-014 owns rendering it. See the notes' open items.
    assert before.json()["candidate"] is None
    assert before.json()["consent"]["version"] == CONSENT_COPY_V1

    retry = await client.post("/v1/avatar-profile/prepare", headers=_headers(user_id))

    assert retry.status_code == 200
    body = retry.json()
    assert body["created"] is True
    assert body["job_id"] == str(job_id), "the same row is re-opened, not a second one"
    assert body["status"] == AvatarStatus.PREPARING.value
    after = await client.get("/v1/avatar-profile", headers=_headers(user_id))
    assert after.json()["status"] == AvatarStatus.PREPARING.value
    await db_session.refresh(job)
    assert job.status == "queued"
    assert job.attempts == 0
    assert await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(AsyncJob)
        .where(AsyncJob.job_type == PREPARATION_JOB_TYPE)
    ) == 1


async def test_prepare_without_consent_is_a_conflict(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    user_id, profile, asset = await _seed_candidate(db_session)

    response = await client.post("/v1/avatar-profile/prepare", headers=_headers(user_id))

    assert response.status_code == 409
    assert response.json()["code"] == 4091
    assert await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(AsyncJob)
        .where(AsyncJob.job_type == PREPARATION_JOB_TYPE)
    ) == 0


async def test_prepare_without_a_profile_is_not_found(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    user_id = await _seed_user(db_session)

    response = await client.post("/v1/avatar-profile/prepare", headers=_headers(user_id))

    assert response.status_code == 404
    assert response.json()["code"] == 4041


# --- DELETE /v1/avatar-profile -------------------------------------------


async def test_delete_requires_the_purge_derived_parameter(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §4.2: required, no default.

    Omitting it must not resolve to a silent choice about somebody's photos —
    V3_SPEC §13.5 requires the caller to be explicit.
    """
    _enable(monkeypatch)
    user_id, profile, asset = await _seed_candidate(db_session)

    response = await client.delete("/v1/avatar-profile", headers=_headers(user_id))

    assert response.status_code == 422


async def test_delete_disables_the_profile_and_schedules_the_purge(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    user_id, profile, asset = await _seed_candidate(
        db_session, source_type=AvatarSourceType.USER_UPLOAD
    )

    response = await client.request(
        "DELETE", "/v1/avatar-profile?purge_derived=true", headers=_headers(user_id)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == AvatarStatus.DISABLED.value
    assert body["purge_derived"] is True
    assert body["scheduled_object_count"] == 1
    assert body["purge_job_id"]
    assert await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(AsyncJob)
        .where(AsyncJob.job_type == PURGE_JOB_TYPE)
    ) == 1


async def test_the_delete_response_carries_no_object_keys(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A count, never the keys (contract §7)."""
    _enable(monkeypatch)
    user_id, profile, asset = await _seed_candidate(
        db_session, source_type=AvatarSourceType.USER_UPLOAD
    )
    key = asset.source_object_key

    response = await client.request(
        "DELETE", "/v1/avatar-profile?purge_derived=false", headers=_headers(user_id)
    )

    assert key not in response.text
    assert response.json()["purge_derived"] is False


async def test_delete_without_a_profile_is_not_found(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(monkeypatch)
    user_id = await _seed_user(db_session)

    response = await client.request(
        "DELETE", "/v1/avatar-profile?purge_derived=true", headers=_headers(user_id)
    )

    assert response.status_code == 404


async def test_after_deletion_the_profile_reads_as_disabled(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tombstone is visible, and nothing is offered back.

    A `disabled` profile must not be prompted back into a candidate sheet by the
    daily flow (V3_SPEC §7.3) — `delete_avatar`'s docstring makes the tombstone
    the suppression once the declined rows are gone.
    """
    _enable(monkeypatch)
    user_id, profile, asset = await _seed_candidate(db_session)
    await client.request(
        "DELETE", "/v1/avatar-profile?purge_derived=true", headers=_headers(user_id)
    )

    body = (await client.get("/v1/avatar-profile", headers=_headers(user_id))).json()

    assert body["status"] == AvatarStatus.DISABLED.value
    assert body["candidate"] is None
    assert body["active_version"] is None
