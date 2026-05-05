import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.common import error_codes
from app.common.enums import FitFeedbackType, FitStatus
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.models.fit import FitFeedback
from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository


class SubmitFitFeedback:
    def __init__(self, repository: SqlAlchemyWardrobeRepository, session: AsyncSession) -> None:
        self.repository = repository
        self.session = session

    async def execute(
        self,
        user_id: uuid.UUID,
        item_id: uuid.UUID,
        outfit_id: uuid.UUID | None,
        feedback_type: str,
        scene: str,
    ) -> tuple[FitFeedback, FitStatus]:
        if feedback_type not in {feedback.value for feedback in FitFeedbackType}:
            raise FriendlyException(
                error_codes.VALIDATION_ERROR, "Invalid fit feedback type", status_code=422
            )

        item = await self.repository.get_for_user(item_id, user_id)
        if item is None:
            raise FriendlyException(
                error_codes.NOT_FOUND, "Wardrobe item not found", status_code=404
            )

        feedback = FitFeedback(
            user_id=user_id,
            item_id=item_id,
            outfit_id=outfit_id,
            feedback_type=feedback_type,
            scene=scene,
            created_at=datetime.now(UTC),
        )
        self.session.add(feedback)

        status_updates = {
            FitFeedbackType.FITS_WELL.value: FitStatus.FITS_WELL,
            FitFeedbackType.TIGHT.value: FitStatus.TIGHT,
            FitFeedbackType.LOOSE.value: FitStatus.LOOSE,
        }
        new_status = status_updates.get(feedback_type)
        if new_status is not None:
            item.fit_status = new_status.value

        await self.session.flush()
        return feedback, FitStatus(item.fit_status)
