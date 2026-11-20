"""Album scan: apply the user's per-garment decisions to a batch of photo imports."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.import_tasks.candidates import confirmed_item_ids, normalize_candidates, store_candidates
from app.application.import_tasks.confirm_import import ConfirmImageImportTask, ConfirmItemInput
from app.application.import_tasks.import_batch import (
    DISMISSED_KEY,
    BatchGroup,
    batch_tasks,
    dismissed_ids,
    importable_candidates,
    read_batch_view,
)
from app.application.wardrobe.wears import record_manual_wear
from app.common import error_codes
from app.common.enums import ImportStatus
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.import_task_repo import SqlAlchemyImportTaskRepository
from app.infrastructure.persistence.repositories.recommendation_repo import SqlAlchemyRecommendationRepository
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository
from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository

GroupAction = Literal["create", "link", "skip"]


@dataclass(frozen=True)
class GroupDecision:
    group_id: uuid.UUID
    action: GroupAction
    title: str | None = None
    category: str | None = None
    color_main: str | None = None
    backfill_wear: bool = False


@dataclass(frozen=True)
class GroupOutcome:
    group_id: uuid.UUID
    action: GroupAction
    item: WardrobeItem | None
    created: bool
    wears_recorded: int


class ConfirmImportBatch:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def execute(
        self, user_id: uuid.UUID, batch_id: uuid.UUID, decisions: list[GroupDecision]
    ) -> list[GroupOutcome]:
        if not decisions or len({d.group_id for d in decisions}) != len(decisions):
            raise FriendlyException(error_codes.VALIDATION_ERROR, "Decide each group at most once", 422)
        # Serialize confirmations of one batch: a retried request then replays
        # the first one's result instead of creating a second item.
        digest = hashlib.sha256(f"import-batch:{user_id}:{batch_id}".encode()).digest()
        lock = int.from_bytes(digest[:8], "big", signed=True)
        await self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})

        view = await read_batch_view(self.session, user_id, batch_id, schedule=False)
        groups: list[tuple[GroupDecision, BatchGroup]] = []
        for decision in decisions:
            group = view.group(decision.group_id)
            if group is None:
                raise FriendlyException(
                    error_codes.STATE_CONFLICT, "Batch groups changed; reload the batch", 409,
                    data={"reason": "unknown_group", "group_id": str(decision.group_id)},
                )
            if decision.action == "skip" and group.status == "confirmed":
                raise FriendlyException(
                    error_codes.STATE_CONFLICT, "Group was already added to the wardrobe", 409,
                    data={"reason": "group_confirmed", "group_id": str(group.group_id)},
                )
            if decision.action == "link" and group.existing_item is None:
                raise FriendlyException(error_codes.VALIDATION_ERROR, "Group has no wardrobe item to link", 422)
            groups.append((decision, group))

        confirm = ConfirmImageImportTask(
            SqlAlchemyImportTaskRepository(self.session),
            SqlAlchemyWardrobeRepository(self.session),
            SqlAlchemyRecommendationRepository(self.session),
            SqlAlchemyStyleRepository(self.session),
        )
        attach: dict[uuid.UUID, dict[str, str]] = {}
        dismiss: dict[uuid.UUID, set[str]] = {}
        outcomes: list[GroupOutcome] = []
        for decision, group in groups:
            item: WardrobeItem | None = None
            created = False
            if decision.action == "skip":
                for member in group.members:
                    dismiss.setdefault(member.task_id, set()).add(member.temp_item_id)
            elif decision.action == "link" or group.status == "confirmed":
                item = group.existing_item
            else:
                best = group.best
                entry = (
                    await confirm.confirm_items(
                        best.task_id, user_id,
                        [ConfirmItemInput(
                            best.temp_item_id,
                            decision.title or best.title,
                            decision.category or best.category,
                            decision.color_main or best.color_main,
                        )],
                    )
                )[0]
                item, created = entry.item, entry.created
            wears = 0
            if item is not None:
                for member in group.members:
                    if member.confirmed_item_id is None:
                        attach.setdefault(member.task_id, {})[member.temp_item_id] = str(item.id)
                if decision.backfill_wear:
                    dates = sorted({m.taken_at for m in group.members if m.taken_at is not None})
                    for worn in dates:
                        wears += await record_manual_wear(self.session, user_id, item.id, worn)
            outcomes.append(GroupOutcome(group.group_id, decision.action, item, created, wears))

        await self._settle_tasks(user_id, batch_id, attach, dismiss)
        return outcomes

    async def _settle_tasks(
        self,
        user_id: uuid.UUID,
        batch_id: uuid.UUID,
        attach: dict[uuid.UUID, dict[str, str]],
        dismiss: dict[uuid.UUID, set[str]],
    ) -> None:
        """Record every member's outcome and close photos with nothing left to decide."""
        for task in await batch_tasks(self.session, user_id, batch_id, for_update=True):
            if task.id not in attach and task.id not in dismiss:
                continue
            confirmed = confirmed_item_ids(task)
            for temp_id, item_id in attach.get(task.id, {}).items():
                confirmed.setdefault(temp_id, item_id)
            dismissed = (dismissed_ids(task) | dismiss.get(task.id, set())) - confirmed.keys()
            candidates = normalize_candidates(task)
            if candidates:
                store_candidates(task, candidates)
            task.input_payload = {
                **task.input_payload, "confirmed_items": confirmed, DISMISSED_KEY: sorted(dismissed),
            }
            undecided = {raw["temp_item_id"] for raw in importable_candidates(task)} - confirmed.keys() - dismissed
            if task.task_status == ImportStatus.WAITING_CONFIRM.value and not undecided:
                task.task_status = ImportStatus.COMPLETED.value
        await self.session.flush()
