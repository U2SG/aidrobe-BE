"""V3-013 — the preparation idempotency key and the pipeline identity behind it.

Container-free by construction: every function under test is pure. These are the
assertions that make acceptance 2 ("Prepare is idempotent and does not duplicate
paid work") checkable without a database, and they cover the failure modes V3-012
found the expensive way — a key that drifts from the code that answers the job,
and a key that outgrows its column.
"""

from __future__ import annotations

import uuid

from app.application.avatar.delete_avatar import JOB_TYPE as PURGE_JOB_TYPE
from app.application.avatar.delete_avatar import purge_idempotency_key
from app.application.avatar.prepare_avatar import (
    JOB_TYPE,
    MAX_ATTEMPTS,
    PreparationMode,
    preparation_idempotency_key,
    preparation_pipeline_identity,
)
from app.config.settings import Settings
from app.infrastructure.persistence.models.async_job import AsyncJob


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def test_same_profile_and_photo_produce_the_same_key() -> None:
    """The whole of "does not duplicate paid work" at the key level."""
    profile_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    identity = preparation_pipeline_identity(_settings())

    first = preparation_idempotency_key(profile_id, asset_id, identity)
    second = preparation_idempotency_key(profile_id, asset_id, identity)

    assert first == second


def test_a_different_consented_photo_is_a_different_key() -> None:
    """`replace` must not be swallowed as a duplicate of the first preparation.

    If the key ignored the source asset, a user replacing their avatar with a new
    photo would get the original job back and their new photo would never be
    prepared — a silent no-op on an explicit user action.
    """
    profile_id = uuid.uuid4()
    identity = preparation_pipeline_identity(_settings())

    assert preparation_idempotency_key(
        profile_id, uuid.uuid4(), identity
    ) != preparation_idempotency_key(profile_id, uuid.uuid4(), identity)


def test_a_different_profile_is_a_different_key() -> None:
    """Two users consenting to byte-identical photos must not share one job."""
    asset_id = uuid.uuid4()
    identity = preparation_pipeline_identity(_settings())

    assert preparation_idempotency_key(
        uuid.uuid4(), asset_id, identity
    ) != preparation_idempotency_key(uuid.uuid4(), asset_id, identity)


def test_a_pipeline_bump_reopens_the_work() -> None:
    """A new model/prompt version is new work, not a cached answer.

    Same reasoning as V3-012's `assessor` key component: when the answer a key
    caches can change, the key has to carry the thing that changes it, or the
    result is permanently pinned to a pipeline nobody runs any more.
    """
    profile_id = uuid.uuid4()
    asset_id = uuid.uuid4()

    assert preparation_idempotency_key(
        profile_id, asset_id, "fake:fake-canonical-1:v1"
    ) != preparation_idempotency_key(profile_id, asset_id, "fake:fake-canonical-2:v1")


def test_pipeline_identity_matches_the_adapter_the_container_selects() -> None:
    """The key must name the pipeline that will actually answer the job.

    `preparation_pipeline_identity` and `container.avatar_preparation` are two
    selectors over the same decision. V3-012's `assessor_identity` has the same
    test for the same reason: a key claiming a pipeline the worker does not run
    makes rows unreachable, because the second job is swallowed as a duplicate of
    an answer that was never produced.
    """
    from app.bootstrap.container import avatar_preparation
    from app.infrastructure.vision.fake_avatar_preparation import (
        MODEL_VERSION,
        PROMPT_VERSION,
        PROVIDER,
    )

    adapter = avatar_preparation()
    assert type(adapter).__name__ == "FakeAvatarPreparation"
    assert preparation_pipeline_identity(_settings()) == f"{PROVIDER}:{MODEL_VERSION}:{PROMPT_VERSION}"


def test_the_key_fits_the_column() -> None:
    """`async_jobs.idempotency_key` is String(128) — a longer key is a 500.

    Pinned rather than trusted: the key embeds a uuid, a digest and a
    provider:model:prompt triple, and the triple is the part a future adapter
    will lengthen. V3-012 hit this budget from the other direction with a
    512-char object key.
    """
    key = preparation_idempotency_key(
        uuid.uuid4(), uuid.uuid4(), preparation_pipeline_identity(_settings())
    )
    limit = AsyncJob.__table__.c.idempotency_key.type.length

    assert key.startswith(f"{JOB_TYPE}:")
    assert limit is not None
    assert len(key) <= limit


def test_a_long_provider_triple_still_fits() -> None:
    """The bound holds with a realistic vendor pipeline name, not just the fake.

    This test caught a real defect on first run. The identity used to be embedded
    verbatim, so a plausible hosted-provider triple produced a **140-character**
    key against a String(128) column — a 500 on the first `prepare` after V3-016
    swaps the adapter, invisible while the only adapter is called `fake`. The
    identity is now digested, which makes the length constant.
    """
    identity = "minimax-video-01:abcdef0123456789-20260814:tryon-prompt-v12"
    key = preparation_idempotency_key(uuid.uuid4(), uuid.uuid4(), identity)
    limit = AsyncJob.__table__.c.idempotency_key.type.length

    assert limit is not None
    assert len(key) <= limit


def test_key_length_does_not_depend_on_the_provider_name() -> None:
    """Fixed length, so no future adapter name can re-break the bound."""
    profile_id = uuid.uuid4()
    asset_id = uuid.uuid4()

    short = preparation_idempotency_key(profile_id, asset_id, "a:b:c")
    long = preparation_idempotency_key(profile_id, asset_id, "x" * 400)

    assert len(short) == len(long)


def test_the_mode_is_not_part_of_the_key() -> None:
    """`prepare` and `replace` over the same photo must charge once, not twice.

    The two endpoints differ in precondition, not in the work they buy. If the
    mode were in the key, a client that called `prepare` and then `replace` on the
    same consented photo would pay for two identical provider runs.
    """
    profile_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    identity = preparation_pipeline_identity(_settings())
    key = preparation_idempotency_key(profile_id, asset_id, identity)

    for mode in PreparationMode:
        assert mode.value not in key


def test_preparation_retries_where_detection_does_not() -> None:
    """The attempt budgets encode opposite consequences, so pin them together.

    A lost `avatar_candidate` job leaves nothing user-visible (V3-012 chose 1
    attempt deliberately). A lost `avatar_preparation` leaves the user stuck in
    `preparing`. If someone later "harmonizes" these two constants, this test says
    why they differ.
    """
    from app.application.avatar.enqueue_candidate_detection import (
        MAX_ATTEMPTS as DETECTION_MAX_ATTEMPTS,
    )

    assert DETECTION_MAX_ATTEMPTS == 1
    assert MAX_ATTEMPTS > DETECTION_MAX_ATTEMPTS


# --- the purge key, which had the same class of defect --------------------
#
# Codex review on BE#147, P1: `avatar_purge:{profile_id}` is not unique to one
# deletion. The profile row survives a delete as a `disabled` tombstone and keeps
# its id, so a second deletion after the frozen `disabled -> preparing` re-enable
# composed the same key, `ON CONFLICT DO NOTHING` returned the SUCCEEDED first
# job, and the rebuilt avatar's objects were never swept — while the response
# reported them as scheduled. Pure function, so this is local evidence.


def test_the_purge_key_separates_two_deletions_with_different_objects() -> None:
    profile_id = uuid.uuid4()
    first = purge_idempotency_key(profile_id, [f"users/{profile_id}/avatar/a.jpg"])
    second = purge_idempotency_key(profile_id, [f"users/{profile_id}/avatar/b.jpg"])

    assert first != second, "a rebuilt avatar's deletion must not reuse a completed job"


def test_the_purge_key_is_stable_for_the_same_deletion() -> None:
    """Retry idempotency is preserved, which is the point of digesting rather
    than counting: two DELETEs over the same objects still collapse to one sweep."""
    profile_id = uuid.uuid4()
    keys = ["users/x/avatar/source.jpg", "users/x/avatar/canonical/front.png"]

    assert purge_idempotency_key(profile_id, keys) == purge_idempotency_key(profile_id, keys)


def test_the_purge_key_does_not_depend_on_key_order() -> None:
    """The plan is assembled from two separate queries (versions, then source
    assets), so an unstable order would mint a new key for an identical deletion
    and re-open the double-sweep it exists to prevent."""
    profile_id = uuid.uuid4()
    keys = ["users/x/avatar/source.jpg", "users/x/avatar/canonical/front.png"]

    assert purge_idempotency_key(profile_id, keys) == purge_idempotency_key(
        profile_id, list(reversed(keys))
    )


def test_the_purge_key_is_the_same_profile_scoped_shape() -> None:
    profile_id = uuid.uuid4()

    key = purge_idempotency_key(profile_id, ["users/x/a.jpg"])

    assert key.startswith(f"{PURGE_JOB_TYPE}:{profile_id}:")


def test_a_huge_deletion_still_fits_the_idempotency_column() -> None:
    """The whole reason the key set is digested rather than embedded: a user
    with many photos would otherwise compose a key far past String(128) and 500
    on the one request that must not fail — a deletion."""
    profile_id = uuid.uuid4()
    keys = [f"users/{profile_id}/avatar/source-{i}.jpg" for i in range(500)]

    key = purge_idempotency_key(profile_id, keys)

    assert len(key) == 82
    assert len(key) <= 128
