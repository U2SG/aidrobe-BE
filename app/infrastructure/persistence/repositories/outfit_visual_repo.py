import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import VisualType
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset


class SqlAlchemyOutfitVisualRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, asset: OutfitVisualAsset) -> OutfitVisualAsset:
        self._session.add(asset)
        await self._session.flush()
        return asset

    async def get_by_id(self, asset_id: uuid.UUID) -> OutfitVisualAsset | None:
        result = await self._session.execute(
            select(OutfitVisualAsset).where(OutfitVisualAsset.id == asset_id)
        )
        return result.scalar_one_or_none()

    async def get_by_outfit_id(self, outfit_id: uuid.UUID) -> list[OutfitVisualAsset]:
        result = await self._session.execute(
            select(OutfitVisualAsset).where(OutfitVisualAsset.outfit_id == outfit_id)
        )
        return list(result.scalars().all())

    async def delete_static_tryon_for_user(
        self, user_id: uuid.UUID
    ) -> tuple[tuple[uuid.UUID, str | None], ...]:
        """Delete this user's try-on renders and say which objects they held.

        Contract §4.2's `purge_derived=true` scope: "generated **try-on** assets
        derived from any avatar version". Selected by `user_id` +
        `visual_type='static_tryon'` rather than by joining an avatar version,
        and the difference matters in exactly one direction. A user has one
        avatar profile, `DELETE /v1/avatar-profile` deletes *every* version of
        it, and a static try-on render is a picture of that avatar by
        construction — so the two sets are the same set today. But a row whose
        `input_snapshot` lost its `avatar_version_id` would drop out of the
        join and stay in the bucket, and "we kept a likeness the user revoked"
        is the failure this parameter exists to prevent. Scoping to the user
        fails the other way: it can only over-delete rows that are the user's
        own renders of their own avatar, which is what they asked for.

        (`input_snapshot->>'avatar_version_id'` *is* written by
        `RequestStaticPreview`, so the per-version linkage exists if a future
        feature needs it. It is not needed to make this parameter effective.)

        Returns `(asset_id, object_key)` per deleted row — `object_key` is NULL
        while a render is still queued or in flight — so the caller can compose
        the storage sweep. Rows go here, objects go to the `avatar_purge` job:
        object deletion is neither transactional nor reversible and must not
        run inside the request transaction.
        """
        rows = (
            await self._session.execute(
                select(OutfitVisualAsset.id, OutfitVisualAsset.object_key).where(
                    OutfitVisualAsset.user_id == user_id,
                    OutfitVisualAsset.visual_type == VisualType.STATIC_TRYON.value,
                )
            )
        ).all()
        if rows:
            await self._session.execute(
                delete(OutfitVisualAsset).where(
                    OutfitVisualAsset.id.in_([asset_id for asset_id, _key in rows])
                )
            )
            await self._session.flush()
        return tuple((asset_id, key) for asset_id, key in rows)

    async def update_status(
        self,
        asset_id: uuid.UUID,
        status: str,
        image_url: str | None = None,
        error_code: str | None = None,
        error: str | None = None,
        object_key: str | None = None,
        quality_signals: dict[str, object] | None = None,
    ) -> None:
        asset = await self.get_by_id(asset_id)
        if asset is None:
            return
        asset.generation_status = status
        if image_url is not None:
            asset.image_url = image_url
        if object_key is not None:
            asset.object_key = object_key
        if error_code is not None:
            asset.error_code = error_code
        if error is not None:
            asset.error_message = error
        if quality_signals is not None:
            asset.quality_signals = quality_signals
        await self._session.flush()
