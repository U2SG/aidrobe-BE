"""V3-017 — the strong cache key, and the job key derived from it.

Contract §6.3 freezes both:

    cache key = (avatar_version_id, normalized_candidate_asset_hash,
                 sorted_supporting_item_ids, view_type,
                 provider_model_version, prompt_version)
    job idempotency key = static_tryon:{sha256(cache_key)}

Pure by construction — no DB, no IO, no SQLAlchemy import — following the
V3-004/005/006/007 split between a module that decides and a module that
queries. `request_static_preview` does the queries.

**Why the key is composed before anything is generated.** Every component is
knowable at enqueue time, and that is the whole point: a key computable only
from a result could never prevent the paid call it exists to prevent. The two
provider components come from `StaticTryOnPort.pipeline_identity`, which V3-016
made a property for exactly this reason.

**`normalized_candidate_asset_hash` hashes the stable object key, never bytes
and never a signed URL.** Signed URLs are re-minted per call with a 30-minute
TTL (contract §3), so hashing one would give the same photograph a different key
every half hour and mint a fresh paid job each time — the failure mode
`docs/async-jobs.md` records verbatim for V3-012's `avatar_candidate` key
("hashed from the *stable object key*, never a signed URL"). Hashing the bytes
would be defensible but costs a storage read on every request, including the
cache hits this exists to serve cheaply, to learn something the key already
knows: `purchase_candidates.source_object_key` is immutable for the life of the
candidate.

**Supporting item ids are sorted, and that is a correctness requirement rather
than tidiness.** Contract §6.3 says `sorted_supporting_item_ids`, so two
requests that differ only in the order the composer emitted the same garments
are one cache entry and one charge. V3-016 made the renderer order-insensitive
from the other side (its strip sorts by slot, then by digest), so the two halves
agree: the same key really does describe the same picture.

**The candidate is not in the supporting list.** It is hashed separately because
it is a different thing to the product — the garment the decision is *about* —
and because folding it into the sorted list would make a candidate-as-supporting
collision possible: two different outfits whose union of ids happens to match.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

#: The frozen key prefix from contract §6.3. Deliberately NOT equal to the
#: `job_type` (`tryon`), which is the quota bucket `cost_guard._limit_for`
#: already knows — see `request_static_preview.JOB_TYPE` for why the two differ.
KEY_PREFIX = "static_tryon"

#: Full SHA-256 hex, 64 chars, so the composed key is a fixed 77 characters
#: against `async_jobs.idempotency_key`'s String(128). V3-013 learned the same
#: budget lesson the hard way (`prepare_avatar._IDENTITY_DIGEST_CHARS`): a
#: realistic vendor triple pushed an *embedded* identity to 140 chars. Nothing
#: here is embedded, so no provider name can re-break the bound.
_DIGEST_CHARS = 64

#: Separates key components. A character that cannot occur inside a uuid, a
#: sha256 hex digest or a `view_type` value, so no combination of components can
#: be re-parsed into a different tuple that renders the same string.
_SEP = "|"


@dataclass(frozen=True)
class StaticPreviewCacheKey:
    """Contract §6.3's six components, in the order §6.3 lists them.

    `avatar_version_id` and not the profile id: a preview is of a *likeness*,
    and replacing the avatar produces a new version, which must produce a new
    picture rather than serve the previous person's render from cache. It is
    also why deleting an avatar cannot orphan a cached preview into a stranger's
    result — the id is gone and the key can never be recomposed.
    """

    avatar_version_id: uuid.UUID
    normalized_candidate_asset_hash: str
    sorted_supporting_item_ids: tuple[uuid.UUID, ...]
    view_type: str
    provider_model_version: str
    prompt_version: str

    def as_tuple(self) -> tuple[str, ...]:
        """The components as strings, for logging and assertions.

        Note what this is not: it is not what gets hashed field by field. The
        digest is taken over `canonical()` so a test can pin one rendering.
        """
        return (
            str(self.avatar_version_id),
            self.normalized_candidate_asset_hash,
            ",".join(str(i) for i in self.sorted_supporting_item_ids),
            self.view_type,
            self.provider_model_version,
            self.prompt_version,
        )

    def canonical(self) -> str:
        """One unambiguous string per distinct set of inputs."""
        return _SEP.join(self.as_tuple())

    def digest(self) -> str:
        """`sha256(cache_key)` — contract §6.3's inner term.

        Stored on `outfit_visual_assets.cache_key`, where a unique index makes
        "same key ⇒ same asset" a database guarantee rather than a convention
        the request path has to remember.
        """
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()[:_DIGEST_CHARS]

    def idempotency_key(self) -> str:
        """`static_tryon:{sha256(cache_key)}` — contract §6.3, verbatim."""
        return f"{KEY_PREFIX}:{self.digest()}"


def candidate_asset_hash(*, source_object_key: str) -> str:
    """Hash the candidate's durable storage reference.

    A `str` parameter rather than the candidate row, so this module stays free
    of ORM imports and a caller cannot accidentally pass a signed URL by
    reaching for the wrong attribute — `source_object_key` is the only name that
    fits.
    """
    if not source_object_key:
        raise ValueError("candidate asset hash needs a non-empty object key")
    return hashlib.sha256(source_object_key.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def build_cache_key(
    *,
    avatar_version_id: uuid.UUID,
    source_object_key: str,
    supporting_item_ids: Iterable[uuid.UUID],
    view_type: str,
    provider: str,
    model_version: str,
    prompt_version: str,
) -> StaticPreviewCacheKey:
    """Compose the frozen key from raw inputs, sorting and de-duplicating.

    De-duplication matters as much as sorting: V3-006 fills at most one garment
    per slot, so a repeated id is not something it can currently produce — but
    if it ever did, `[a, a, b]` and `[a, b]` describe the same picture (V3-016's
    strip renders each garment once per slot) and must not be two paid jobs.
    Sorting by the uuid's canonical string is stable across processes, which
    `sorted()` on the uuid objects would also be; the string form is used so the
    ordering is the one a reader of `canonical()` can verify by eye.

    `provider` and `model_version` are joined into contract §6.3's single
    `provider_model_version` component rather than kept apart, because that is
    the component the contract names. Keeping them separate would be a
    seven-component key claiming to be the frozen six.
    """
    unique_ids = {str(i): i for i in supporting_item_ids}
    ordered = tuple(unique_ids[k] for k in sorted(unique_ids))
    return StaticPreviewCacheKey(
        avatar_version_id=avatar_version_id,
        normalized_candidate_asset_hash=candidate_asset_hash(source_object_key=source_object_key),
        sorted_supporting_item_ids=ordered,
        view_type=view_type,
        provider_model_version=f"{provider}:{model_version}",
        prompt_version=prompt_version,
    )
