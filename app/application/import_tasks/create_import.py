import hashlib
import uuid

from sqlalchemy import select, text

from app.application.import_tasks.source import upload_key
from app.common import error_codes
from app.common.enums import ImportStatus
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.repositories.import_task_repo import (
    SqlAlchemyImportTaskRepository,
)
from app.infrastructure.storage.source_image import InvalidSourceImage


class CreateImageImportTask:
    def __init__(
        self,
        repository: SqlAlchemyImportTaskRepository,
    ) -> None:
        self.repository = repository

    async def execute(
        self,
        user_id: uuid.UUID,
        input_type: str,
        source_type: str,
        image_url: str | None = None,
        source_url: str | None = None,
        locale: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[ImportTask, bool]:
        """Returns (task, created). With an `Idempotency-Key`, a retried request
        (double tap, flaky network) gets the task the first attempt created
        instead of a second task that would later confirm into a duplicate item."""
        if idempotency_key:
            existing = await self._find_by_key(user_id, idempotency_key)
            if existing is not None:
                return existing, False

        # BE#30 — persist the request locale hint so the (background) worker
        # can name the candidate in it. None = fall back to user settings.
        input_payload: dict[str, object] = {"image_url": image_url, "source_url": source_url, "locale": locale}
        try:
            key = upload_key(image_url, user_id)
            if image_url is None and upload_key(source_url, user_id):
                raise InvalidSourceImage("Internal asset cannot be used as a product page")
        except InvalidSourceImage:
            raise FriendlyException(error_codes.VALIDATION_ERROR, "Invalid import image or link", 422) from None
        if key is not None:
            input_payload["source_object_key"] = key
            # Preserve a stable URL for old readers only. The key owns reads.
            input_payload["image_url"] = image_url.split("?", 1)[0].split("#", 1)[0] if image_url else None
        if idempotency_key:
            input_payload["idempotency_key"] = idempotency_key

        task = await self.repository.add(
            ImportTask(
                user_id=user_id,
                input_type=input_type,
                source_type=source_type,
                task_status=ImportStatus.QUEUED.value,
                input_payload=input_payload,
            )
        )
        return task, True

    async def _find_by_key(self, user_id: uuid.UUID, key: str) -> ImportTask | None:
        session = self.repository.session
        # Serialize same-key creates for this user until commit, so two
        # concurrent retries can't both miss the lookup and insert twice.
        lock = int.from_bytes(
            hashlib.sha1(f"import-create:{user_id}:{key}".encode()).digest()[:8], "big", signed=True
        )
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
        found: ImportTask | None = await session.scalar(
            select(ImportTask)
            .where(
                ImportTask.user_id == user_id,
                ImportTask.input_payload["idempotency_key"].astext == key,
            )
            .order_by(ImportTask.created_at)
            .limit(1)
        )
        return found
