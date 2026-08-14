"""V3-012 — candidate detection against a real Postgres.

What only a database can prove, and is therefore here rather than in the unit
fixture matrix:

- an offer really does leave consent alone (acceptance 1) — the row is re-read
  from Postgres after the transaction, not inspected in memory;
- the suppression matrix actually suppresses, including the cross-asset
  "已选暂不用" rule that is a query rather than a field;
- `one_row_per_source_object` closes the concurrent-detection race, and the
  SAVEPOINT keeps the violation from poisoning the session;
- the `pending` / `rejected` distinction survives persistence, so a photo that
  was never examined is not indistinguishable from one that was refused.

The detector is always a fake here. `MaskPersonFrameDetector` needs the optional
rembg/onnxruntime extras, which are absent in CI — so the OFFERED path is
exercised through an injected observation, exactly the shape of V3-005's
pgvector-off limit. What is *not* proven anywhere in this suite: that the real
segmentation model finds a person in a real photograph.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.avatar.detect_candidate import (
    CandidateDetectionOutcome,
    CandidateDetectionTrigger,
    DetectAvatarCandidate,
    SkipReason,
)
from app.application.ports.avatar_detection import PersonFrameObservation
from app.common.enums import (
    AvatarConsentStatus,
    AvatarQualityStatus,
    AvatarSourceType,
    AvatarStatus,
)
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.avatar import (
    AvatarProfile,
    AvatarSourceAsset,
    AvatarVersion,
)
from app.infrastructure.persistence.models.user import User

OBJECT_KEY = "users/photo-a/ootd/one.jpg"
OTHER_KEY = "users/photo-a/ootd/two.jpg"

GOOD_OBSERVATION = PersonFrameObservation(
    subject_region_count=1,
    subject_frame_fill_ratio=0.35,
    subject_vertical_span_ratio=0.9,
    top_edge_contact_ratio=0.0,
    sharpness_score=0.6,
    pixel_width=1080,
    pixel_height=1920,
    detector="fake_detector",
)
TWO_PEOPLE = PersonFrameObservation(
    subject_region_count=2,
    subject_frame_fill_ratio=0.4,
    subject_vertical_span_ratio=0.9,
    top_edge_contact_ratio=0.0,
    sharpness_score=0.6,
    pixel_width=1080,
    pixel_height=1920,
    detector="fake_detector",
)


class _FakeStorage:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.reads: list[str] = []

    async def get_object(self, key: str) -> bytes:
        self.reads.append(key)
        if self.fail:
            raise RuntimeError("object missing")
        return b"not-a-real-image"


class _FakeDetector:
    def __init__(self, observation: PersonFrameObservation | None, *, raises: bool = False) -> None:
        self.observation = observation
        self.raises = raises
        self.calls = 0

    async def observe(self, image_bytes: bytes) -> PersonFrameObservation:
        self.calls += 1
        if self.raises:
            raise RuntimeError("detector unavailable")
        assert self.observation is not None
        return self.observation


def _settings(*, enabled: bool = True) -> Settings:
    return get_settings().model_copy(update={"ENABLE_V3_AVATAR": enabled})


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    return user_id


async def _seed_profile(
    session: AsyncSession, user_id: uuid.UUID, status: AvatarStatus
) -> AvatarProfile:
    """A profile in `status`, satisfying the CHECK constraints V3-011 attached
    to that status.

    Two of them are database constraints, not conventions, and a first cut of this
    test violated both — Postgres correctly refused the rows:

    - `(status IN (ready, refresh_recommended)) = (active_version_id IS NOT NULL)`
      means those two states cannot exist without a real version;
    - `(status = 'disabled') = (disabled_at IS NOT NULL)` means a revocation
      always records when it happened.

    Seeding them properly keeps the suppression matrix a test of *suppression*
    rather than an accidental test of V3-011's constraints.
    """
    profile = AvatarProfile(
        user_id=user_id,
        status=status.value,
        disabled_at=datetime.now(UTC) if status is AvatarStatus.DISABLED else None,
    )
    session.add(profile)
    await session.flush()
    if status in {AvatarStatus.READY, AvatarStatus.REFRESH_RECOMMENDED}:
        version = AvatarVersion(
            avatar_profile_id=profile.id,
            source_asset_ids=[],
            provider="fake",
            model_version="test-model-1",
            prompt_version="test-prompt-1",
            canonical_front_object_key="users/x/avatar/front.png",
        )
        session.add(version)
        await session.flush()
        profile.active_version_id = version.id
        await session.flush()
    return profile


def _use_case(
    session: AsyncSession,
    *,
    observation: PersonFrameObservation | None = GOOD_OBSERVATION,
    enabled: bool = True,
    storage: _FakeStorage | None = None,
    detector: _FakeDetector | None = None,
) -> DetectAvatarCandidate:
    return DetectAvatarCandidate(
        session,
        storage=storage or _FakeStorage(),  # type: ignore[arg-type]
        detector=detector or _FakeDetector(observation),  # type: ignore[arg-type]
        settings=_settings(enabled=enabled),
    )


# --- acceptance 1: detection never changes consent state -------------------


async def test_an_offer_records_material_without_granting_consent(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)

    result = await _use_case(db_session).execute(
        user_id=user_id,
        source_type=AvatarSourceType.OOTD_PHOTO,
        object_key=OBJECT_KEY,
    )
    assert result.outcome is CandidateDetectionOutcome.OFFERED

    # Re-read from Postgres rather than trusting the in-memory objects.
    db_session.expire_all()
    asset = await db_session.get(AvatarSourceAsset, result.source_asset_id)
    assert asset is not None
    assert asset.quality_status == AvatarQualityStatus.PASSED.value
    # The whole of acceptance 1, on the row that actually exists:
    assert asset.consent_status == AvatarConsentStatus.PENDING.value
    assert asset.consent_decided_at is None
    assert asset.consent_version is None

    profile = await db_session.scalar(sa.select(AvatarProfile).where(AvatarProfile.user_id == user_id))
    assert profile is not None
    # "qualified material exists, not yet consented" (contract §4.1).
    assert profile.status == AvatarStatus.CANDIDATE.value
    assert profile.consent_version is None
    assert profile.consented_at is None


async def test_the_detection_module_contains_no_write_to_a_consent_field() -> None:
    """A source-level pin on acceptance 1, so an edit that granted consent from
    the detection path fails here even if it happened to satisfy the behavioural
    test above.

    Checked over the parsed AST, not the source text. A text scan looked
    sufficient and was not: the module's own docstring contains the string
    `consent_status='pending'` while explaining that the column default supplies
    it, so a substring match either fires on prose or has to be loosened until it
    stops catching `consent_status=` written without spaces. The AST has no such
    ambiguity — it sees assignments, keyword arguments and `setattr` calls, and
    cannot see a docstring at all.

    Three edit shapes are covered: assigning a consent attribute, passing a
    consent keyword to a constructor (which would set it explicitly instead of
    relying on the `pending` default), and reaching it through `setattr`.

    This complements rather than replaces the real guarantee, which lives in the
    signature: `offer_candidate` has no consent parameter, so there is no
    argument through which detection could grant permission.
    """
    import ast

    fields = {"consent_status", "consent_version", "consent_decided_at", "consented_at"}
    tree = ast.parse(Path("app/application/avatar/detect_candidate.py").read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute):
                assert target.attr not in fields, f"assigns {target.attr}"
            if isinstance(target, ast.Name):
                assert target.id not in fields, f"assigns {target.id}"

        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                assert keyword.arg not in fields, f"passes {keyword.arg} explicitly"
            callee = node.func
            if isinstance(callee, ast.Name) and callee.id == "setattr":
                literals = [a.value for a in node.args if isinstance(a, ast.Constant)]
                assert not (fields & set(literals)), "setattr on a consent field"


async def test_a_rejected_photo_is_recorded_but_never_offered(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)

    result = await _use_case(db_session, observation=TWO_PEOPLE).execute(
        user_id=user_id,
        source_type=AvatarSourceType.OOTD_PHOTO,
        object_key=OBJECT_KEY,
    )
    assert result.outcome is CandidateDetectionOutcome.NOT_USABLE

    db_session.expire_all()
    asset = await db_session.get(AvatarSourceAsset, result.source_asset_id)
    assert asset is not None
    assert asset.quality_status == AvatarQualityStatus.REJECTED.value
    assert asset.consent_status == AvatarConsentStatus.PENDING.value
    # No profile was created: there is nothing to offer, so there is no state
    # to move. A `none`-equivalent user stays exactly as they were.
    assert await db_session.scalar(sa.select(AvatarProfile).where(AvatarProfile.user_id == user_id)) is None


async def test_an_unassessable_photo_is_pending_not_rejected(db_session: AsyncSession) -> None:
    """The detector raised. The row must say "not determined", because
    `rejected` would permanently retire material nobody examined."""
    user_id = await _seed_user(db_session)
    detector = _FakeDetector(None, raises=True)

    result = await _use_case(db_session, detector=detector).execute(
        user_id=user_id,
        source_type=AvatarSourceType.OOTD_PHOTO,
        object_key=OBJECT_KEY,
    )
    assert result.outcome is CandidateDetectionOutcome.UNDETERMINED
    assert detector.calls == 1

    db_session.expire_all()
    asset = await db_session.get(AvatarSourceAsset, result.source_asset_id)
    assert asset is not None
    assert asset.quality_status == AvatarQualityStatus.PENDING.value
    assert asset.quality_signals is not None
    assert asset.quality_signals["detector"] == "unavailable"


async def test_an_unreadable_object_records_nothing_at_all(db_session: AsyncSession) -> None:
    """Distinct from an unavailable detector: no row, so the photo stays
    eligible for a later natural flow instead of burning its one row on a
    transient storage miss."""
    user_id = await _seed_user(db_session)
    storage = _FakeStorage(fail=True)

    result = await _use_case(db_session, storage=storage).execute(
        user_id=user_id,
        source_type=AvatarSourceType.OOTD_PHOTO,
        object_key=OBJECT_KEY,
    )
    assert result.outcome is CandidateDetectionOutcome.SKIPPED
    assert result.reason is SkipReason.IMAGE_UNAVAILABLE
    assert (await db_session.scalars(sa.select(AvatarSourceAsset))).all() == []


# --- acceptance 4: prompt-once ---------------------------------------------


async def test_the_same_photo_is_never_assessed_twice(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    first = await _use_case(db_session).execute(
        user_id=user_id, source_type=AvatarSourceType.OOTD_PHOTO, object_key=OBJECT_KEY
    )
    assert first.outcome is CandidateDetectionOutcome.OFFERED

    detector = _FakeDetector(GOOD_OBSERVATION)
    storage = _FakeStorage()
    second = await _use_case(db_session, detector=detector, storage=storage).execute(
        user_id=user_id, source_type=AvatarSourceType.OOTD_PHOTO, object_key=OBJECT_KEY
    )
    assert second.outcome is CandidateDetectionOutcome.SKIPPED
    assert second.reason is SkipReason.ALREADY_ASSESSED
    # Short-circuited before any work: no storage read and no segmentation.
    assert storage.reads == []
    assert detector.calls == 0
    assert len((await db_session.scalars(sa.select(AvatarSourceAsset))).all()) == 1


async def test_a_declined_photo_silences_natural_flows_for_other_photos_too(
    db_session: AsyncSession,
) -> None:
    """V3_SPEC §7.3's *second* rule, and the one per-asset uniqueness does not
    give you: after "暂不用", the next distinct OOTD photo must not re-open the
    question. Without this a user who declined once would be asked again
    tomorrow, and the day after — the 反复提醒 the spec forbids.
    """
    user_id = await _seed_user(db_session)
    declined = AvatarSourceAsset(
        user_id=user_id,
        source_type=AvatarSourceType.OOTD_PHOTO.value,
        source_object_key=OBJECT_KEY,
        quality_status=AvatarQualityStatus.PASSED.value,
        consent_status=AvatarConsentStatus.DECLINED.value,
        consent_decided_at=sa.func.now(),
    )
    db_session.add(declined)
    await db_session.flush()

    storage = _FakeStorage()
    detector = _FakeDetector(GOOD_OBSERVATION)
    result = await _use_case(db_session, detector=detector, storage=storage).execute(
        user_id=user_id,
        source_type=AvatarSourceType.OOTD_PHOTO,
        object_key=OTHER_KEY,  # a DIFFERENT photo
    )
    assert result.outcome is CandidateDetectionOutcome.SKIPPED
    assert result.reason is SkipReason.DECLINED_BY_USER
    # Suppressed before the photo is read, so a quiet flow costs no segmentation.
    assert storage.reads == []
    assert detector.calls == 0
    assert len((await db_session.scalars(sa.select(AvatarSourceAsset))).all()) == 1


async def test_a_user_initiated_preview_may_ask_again_after_a_decline(
    db_session: AsyncSession,
) -> None:
    """The stated exception — V3_SPEC §7.3's 未来仅在用户主动要求上身预演时重新
    说明. The same fixture as the test above, one argument different."""
    user_id = await _seed_user(db_session)
    db_session.add(
        AvatarSourceAsset(
            user_id=user_id,
            source_type=AvatarSourceType.OOTD_PHOTO.value,
            source_object_key=OBJECT_KEY,
            quality_status=AvatarQualityStatus.PASSED.value,
            consent_status=AvatarConsentStatus.DECLINED.value,
            consent_decided_at=sa.func.now(),
        )
    )
    await db_session.flush()

    result = await _use_case(db_session).execute(
        user_id=user_id,
        source_type=AvatarSourceType.USER_UPLOAD,
        object_key=OTHER_KEY,
        trigger=CandidateDetectionTrigger.PREVIEW_REQUEST,
    )
    assert result.outcome is CandidateDetectionOutcome.OFFERED


async def test_even_a_preview_request_cannot_re_offer_the_declined_photo_itself(
    db_session: AsyncSession,
) -> None:
    """The exception widens *which users* may be asked, never which photos.
    Contract §4.2: "accept=false dismisses that candidate permanently"."""
    user_id = await _seed_user(db_session)
    db_session.add(
        AvatarSourceAsset(
            user_id=user_id,
            source_type=AvatarSourceType.OOTD_PHOTO.value,
            source_object_key=OBJECT_KEY,
            quality_status=AvatarQualityStatus.PASSED.value,
            consent_status=AvatarConsentStatus.DECLINED.value,
            consent_decided_at=sa.func.now(),
        )
    )
    await db_session.flush()

    result = await _use_case(db_session).execute(
        user_id=user_id,
        source_type=AvatarSourceType.USER_UPLOAD,
        object_key=OBJECT_KEY,
        trigger=CandidateDetectionTrigger.PREVIEW_REQUEST,
    )
    assert result.outcome is CandidateDetectionOutcome.SKIPPED
    assert result.reason is SkipReason.ALREADY_ASSESSED


@pytest.mark.parametrize(
    ("status", "expected_reason"),
    [
        (AvatarStatus.CANDIDATE, SkipReason.CANDIDATE_ALREADY_OFFERED),
        (AvatarStatus.PREPARING, SkipReason.AVATAR_ALREADY_ESTABLISHED),
        (AvatarStatus.READY, SkipReason.AVATAR_ALREADY_ESTABLISHED),
        (AvatarStatus.REFRESH_RECOMMENDED, SkipReason.AVATAR_ALREADY_ESTABLISHED),
        (AvatarStatus.DISABLED, SkipReason.REVOKED_BY_USER),
    ],
)
async def test_a_natural_flow_stays_quiet_in_every_non_none_profile_state(
    db_session: AsyncSession, status: AvatarStatus, expected_reason: SkipReason
) -> None:
    user_id = await _seed_user(db_session)
    await _seed_profile(db_session, user_id, status)

    storage = _FakeStorage()
    result = await _use_case(db_session, storage=storage).execute(
        user_id=user_id, source_type=AvatarSourceType.OOTD_PHOTO, object_key=OBJECT_KEY
    )
    assert result.outcome is CandidateDetectionOutcome.SKIPPED
    assert result.reason is expected_reason
    assert storage.reads == []
    assert (await db_session.scalars(sa.select(AvatarSourceAsset))).all() == []


async def test_a_preview_request_reopens_only_the_disabled_state(db_session: AsyncSession) -> None:
    """`disabled` is the one state the trigger changes. `preparing` must stay
    suppressed even for a user-initiated preview — the frozen transition list
    (contract §4.1) has no `preparing → candidate` edge from detection, and
    inventing one would let this interrupt a running preparation."""
    user_id = await _seed_user(db_session)
    await _seed_profile(db_session, user_id, AvatarStatus.PREPARING)

    result = await _use_case(db_session).execute(
        user_id=user_id,
        source_type=AvatarSourceType.USER_UPLOAD,
        object_key=OBJECT_KEY,
        trigger=CandidateDetectionTrigger.PREVIEW_REQUEST,
    )
    assert result.reason is SkipReason.AVATAR_ALREADY_ESTABLISHED


async def test_the_flag_off_does_no_work_and_writes_nothing(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    storage = _FakeStorage()
    detector = _FakeDetector(GOOD_OBSERVATION)

    result = await _use_case(db_session, enabled=False, storage=storage, detector=detector).execute(
        user_id=user_id, source_type=AvatarSourceType.OOTD_PHOTO, object_key=OBJECT_KEY
    )
    assert result.outcome is CandidateDetectionOutcome.SKIPPED
    assert result.reason is SkipReason.FEATURE_DISABLED
    assert storage.reads == []
    assert detector.calls == 0
    assert (await db_session.scalars(sa.select(AvatarSourceAsset))).all() == []
    assert (await db_session.scalars(sa.select(AvatarProfile))).all() == []


# --- the race, and one user's photo never reaching another's profile -------


async def test_a_concurrent_second_detection_of_one_photo_loses_on_the_unique_index(
    db_session: AsyncSession,
) -> None:
    """`one_row_per_source_object` is what makes prompt-once race-safe rather
    than merely conventional. The SAVEPOINT must contain the 23505 so the
    session survives — the V3-007 precedent. Not a check-then-insert guard: a
    duplicate here is a concurrent write, not a defect.

    Limit of this proof, stated: the duplicate is forced by pre-inserting the
    row, because two real sessions cannot be made to interleave deterministically
    inside one test transaction. What it proves is that the violation is
    contained, the loser reports ALREADY_ASSESSED and the session is still
    usable afterwards — not that the read-then-insert ordering is race-free
    end to end.
    """
    user_id = await _seed_user(db_session)
    db_session.add(
        AvatarSourceAsset(
            user_id=user_id,
            source_type=AvatarSourceType.OOTD_PHOTO.value,
            source_object_key=OBJECT_KEY,
            quality_status=AvatarQualityStatus.PASSED.value,
        )
    )
    await db_session.flush()

    use_case = _use_case(db_session)
    # Bypass the pre-read to reach the insert, i.e. stand where the loser of a
    # real race stands.
    asset = await use_case._record(
        user_id=user_id,
        source_type=AvatarSourceType.OOTD_PHOTO,
        object_key=OBJECT_KEY,
        quality_status=AvatarQualityStatus.PASSED,
        signals={"signals_version": "v3-012.1"},
    )
    assert asset is None

    # The session is still usable — the point of the SAVEPOINT.
    assert len((await db_session.scalars(sa.select(AvatarSourceAsset))).all()) == 1
    assert await db_session.scalar(sa.select(sa.literal(1))) == 1


async def test_one_users_photo_cannot_be_offered_on_another_users_profile(
    db_session: AsyncSession,
) -> None:
    """`offer_candidate`'s ownership precondition, exercised rather than read.
    The same object key belongs to two users, which the unique index permits
    (it is scoped per user) — so the guard is the only thing standing here."""
    from app.infrastructure.persistence.repositories.avatar_repo import AvatarLifecycle

    owner = await _seed_user(db_session)
    stranger = await _seed_user(db_session)
    asset = AvatarSourceAsset(
        user_id=owner,
        source_type=AvatarSourceType.OOTD_PHOTO.value,
        source_object_key=OBJECT_KEY,
        quality_status=AvatarQualityStatus.PASSED.value,
    )
    db_session.add(asset)
    stranger_profile = AvatarProfile(user_id=stranger, status=AvatarStatus.NONE.value)
    db_session.add(stranger_profile)
    await db_session.flush()

    with pytest.raises(ValueError, match="different user"):
        await AvatarLifecycle(db_session).offer_candidate(stranger_profile, asset)
    assert stranger_profile.status == AvatarStatus.NONE.value


async def test_rejected_material_can_never_become_an_offer(db_session: AsyncSession) -> None:
    """The second `offer_candidate` precondition: a photo the rules refused
    cannot become the thing a user is asked about, even if a future caller
    passes it in by mistake."""
    from app.infrastructure.persistence.repositories.avatar_repo import AvatarLifecycle

    user_id = await _seed_user(db_session)
    asset = AvatarSourceAsset(
        user_id=user_id,
        source_type=AvatarSourceType.OOTD_PHOTO.value,
        source_object_key=OBJECT_KEY,
        quality_status=AvatarQualityStatus.REJECTED.value,
    )
    db_session.add(asset)
    profile = AvatarProfile(user_id=user_id, status=AvatarStatus.NONE.value)
    db_session.add(profile)
    await db_session.flush()

    with pytest.raises(ValueError, match="passed the quality assessment"):
        await AvatarLifecycle(db_session).offer_candidate(profile, asset)
    assert profile.status == AvatarStatus.NONE.value
