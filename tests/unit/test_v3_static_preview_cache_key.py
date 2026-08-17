"""V3-017 — the strong cache key is what makes acceptance 1 true.

`static_preview_cache_key` is pure, so this whole file runs with no database,
no container and no network (`pytest --noconftest`). That matters beyond speed:
acceptance 1 ("Same avatar version and outfit inputs do not create duplicate
paid jobs") is *decided* here and only *enforced* in Postgres, so the decision
is worth pinning where it can be read without infrastructure.

Two directions are asserted throughout, because either alone is half a cache:
inputs that mean the same thing must collide, and inputs that mean different
things must not.
"""

from __future__ import annotations

import uuid

import pytest

from app.application.purchase.static_preview_cache_key import (
    KEY_PREFIX,
    build_cache_key,
    candidate_asset_hash,
)

AVATAR = uuid.UUID("11111111-1111-1111-1111-111111111111")
ITEM_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
ITEM_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
ITEM_C = uuid.UUID("cccccccc-0000-0000-0000-000000000003")

_BASE = {
    "avatar_version_id": AVATAR,
    "source_object_key": "users/u1/purchase/candidate.png",
    "supporting_item_ids": [ITEM_A, ITEM_B],
    "view_type": "static_tryon",
    "provider": "fake",
    "model_version": "v1",
    "prompt_version": "v1",
}


def _key(**overrides: object):  # type: ignore[no-untyped-def]
    return build_cache_key(**{**_BASE, **overrides})  # type: ignore[arg-type]


def test_the_same_inputs_always_produce_the_same_key() -> None:
    assert _key().digest() == _key().digest()


def test_garment_order_does_not_change_the_key() -> None:
    """Contract §6.3 says `sorted_supporting_item_ids`, and this is why.

    V3-006 emits slots in `FILL_ORDER`, but nothing in the contract promises a
    caller assembles them that way. If order mattered, the same outfit submitted
    twice could be two paid renders of one picture — and V3-016 already made the
    *renderer* order-insensitive, so the two halves have to agree or the key
    would be finer-grained than the image it names.
    """
    assert _key(supporting_item_ids=[ITEM_A, ITEM_B]).digest() == (
        _key(supporting_item_ids=[ITEM_B, ITEM_A]).digest()
    )


def test_a_repeated_garment_is_the_same_outfit() -> None:
    """`[a, a, b]` and `[a, b]` render identically, so they are one job.

    Unreachable from V3-006 today (one garment per slot), which is exactly why
    it is asserted: the de-duplication is invisible in normal operation and
    would be easy to drop in a refactor.
    """
    assert _key(supporting_item_ids=[ITEM_A, ITEM_A, ITEM_B]).digest() == (
        _key(supporting_item_ids=[ITEM_A, ITEM_B]).digest()
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("avatar_version_id", uuid.UUID("99999999-9999-9999-9999-999999999999")),
        ("source_object_key", "users/u1/purchase/other.png"),
        ("supporting_item_ids", [ITEM_A, ITEM_C]),
        ("view_type", "outfit_multiview"),
        ("provider", "someone-else"),
        ("model_version", "v2"),
        ("prompt_version", "v2"),
    ],
)
def test_every_component_changes_the_key(field: str, value: object) -> None:
    """All six §6.3 components are load-bearing — none is decoration.

    Parametrized over each in turn rather than asserted once on a wholly
    different input, because a key that ignored (say) `prompt_version` would
    still pass a single "different inputs differ" check while silently serving
    a stale render after a prompt change.
    """
    assert _key(**{field: value}).digest() != _key().digest()


def test_a_new_avatar_version_never_serves_the_previous_persons_render() -> None:
    """The sharpest case of the above, stated on its own.

    `replace` mints a new `avatar_versions` row, so a preview generated for the
    old likeness must not be handed back for the new one. This is the reason the
    key names the version and not the profile.
    """
    other = uuid.UUID("22222222-2222-2222-2222-222222222222")
    assert _key(avatar_version_id=other).digest() != _key().digest()


def test_the_candidate_is_not_interchangeable_with_a_supporting_item() -> None:
    """Two outfits with the same *union* of garments are still two pictures.

    Folding the candidate into the sorted list would make these collide, and a
    user comparing "this jacket with those trousers" against "those trousers
    with that jacket" would be shown one render for both.
    """
    candidate_a = _key(
        source_object_key="users/u1/purchase/jacket.png", supporting_item_ids=[ITEM_A]
    )
    candidate_b = _key(
        source_object_key="users/u1/purchase/trousers.png", supporting_item_ids=[ITEM_B]
    )
    assert candidate_a.digest() != candidate_b.digest()


def test_the_idempotency_key_is_the_frozen_contract_string() -> None:
    key = _key()
    assert key.idempotency_key() == f"{KEY_PREFIX}:{key.digest()}"
    assert key.idempotency_key().startswith("static_tryon:")


def test_the_idempotency_key_fits_the_column_whatever_a_provider_is_called() -> None:
    """`async_jobs.idempotency_key` is String(128).

    V3-013 shipped this bug and caught it late: an *embedded* vendor triple
    (`minimax-video-01:<dated-model>:tryon-prompt-v12`) pushed its key to 140
    characters, which would have been a 500 on the first request after a
    provider swap. Nothing is embedded here, so the assertion is that the length
    is not merely under the bound but *constant* — a bound that depends on a
    provider's name can be re-broken by naming.
    """
    short = _key()
    long = _key(
        provider="some-extremely-long-vendor-name-with-region-suffix-apac",
        model_version="2026-08-17-preview-high-fidelity-full-body",
        prompt_version="static-tryon-prompt-revision-twelve",
    )
    assert len(short.idempotency_key()) == len(long.idempotency_key())
    assert len(long.idempotency_key()) <= 128
    # 12 for the prefix + 1 separator + 64 hex.
    assert len(long.idempotency_key()) == 77


def test_the_digest_fits_the_cache_key_column() -> None:
    """`outfit_visual_assets.cache_key` is String(64) — an exact fit.

    A digest longer than the column would be silently truncated by some
    drivers, turning two distinct keys into one collision that serves the wrong
    picture. Asserted as equality, not `<=`.
    """
    assert len(_key().digest()) == 64


def test_the_candidate_hash_is_taken_over_the_stable_key_not_a_signed_url() -> None:
    """Re-signing must not mint a new job for the same photograph.

    The same failure `docs/async-jobs.md` records for `avatar_candidate`. The
    two URLs below are the same object with different 30-minute signatures; the
    hash is over the object key, so it cannot see them at all.
    """
    stable = "users/u1/purchase/candidate.png"
    assert candidate_asset_hash(source_object_key=stable) == candidate_asset_hash(
        source_object_key=stable
    )
    signed_now = f"https://cdn.example/{stable}?sig=aaa&expires=1"
    signed_later = f"https://cdn.example/{stable}?sig=bbb&expires=2"
    assert candidate_asset_hash(source_object_key=signed_now) != candidate_asset_hash(
        source_object_key=signed_later
    )


def test_an_empty_candidate_reference_is_refused_rather_than_hashed() -> None:
    """Hashing `""` would give every image-less candidate one shared key.

    They would then all collide into a single asset, and the first render would
    be served for every unrelated link candidate afterwards.
    """
    with pytest.raises(ValueError):
        candidate_asset_hash(source_object_key="")


def test_the_key_carries_exactly_the_six_frozen_components() -> None:
    """Contract §6.3's tuple, in §6.3's order, with nothing extra.

    `provider` and `model_version` are joined into the single
    `provider_model_version` component the contract names; keeping them apart
    would be a seven-component key claiming to be the frozen six.
    """
    key = _key()
    assert key.as_tuple() == (
        str(AVATAR),
        candidate_asset_hash(source_object_key=_BASE["source_object_key"]),  # type: ignore[arg-type]
        f"{ITEM_A},{ITEM_B}",
        "static_tryon",
        "fake:v1",
        "v1",
    )
    assert len(key.as_tuple()) == 6


def test_components_cannot_be_re_parsed_into_a_different_tuple() -> None:
    """The separator must not be forgeable out of component content.

    A `view_type` containing the separator could otherwise shift the boundary
    and make two genuinely different inputs render the same canonical string.
    Asserted by construction: no component may contain it.
    """
    for component in _key().as_tuple():
        assert "|" not in component


def test_an_outfit_with_no_supporting_items_still_keys() -> None:
    """A candidate alone is a legal render (nothing else in the wardrobe fits).

    The empty component must not collapse into the neighbouring one — checked by
    asserting it differs from a one-garment outfit rather than merely that it
    does not raise.
    """
    empty = _key(supporting_item_ids=[])
    assert empty.digest() != _key(supporting_item_ids=[ITEM_A]).digest()
    assert empty.sorted_supporting_item_ids == ()
