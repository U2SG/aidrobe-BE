import uuid

from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository


class AddPdfStyleSource:
    """M19 — create the parent `UserStyleProfile` row for a PDF magazine ingest.

    The row's id is the user-visible `source_id`; the PDF URL is stored in
    `source_url`. Per-page child rows are created later by `pdf_worker`.
    Note: this use-case is only called when the router has already confirmed
    ENABLE_PDF_STYLE_INGEST=true — the flag gates the entire request, so no row
    is created when the flag is off."""

    def __init__(self, repository: SqlAlchemyStyleRepository) -> None:
        self.repository = repository

    async def execute(self, user_id: uuid.UUID, file_url: str) -> UserStyleProfile:
        profile = UserStyleProfile(
            user_id=user_id,
            source_type="pdf_magazine",
            source_url=file_url,
            weight=1.0,
            learn_status="queued",
        )
        return await self.repository.add(profile)
