"""V3-004 — purchase candidate ingestion router (contract §5.1, §5.2).

Two surfaces: create a candidate from a link / screenshot / order image, and
poll it. The decision surfaces (`POST …/{id}/decision`,
`GET /v1/wardrobe-decisions/{id}`) belong to V3-007 / V3-008.

Gated by `ENABLE_V3_PURCHASE_DECISION`, default off → 503 (contract §2 rollback
rule 1, the `ENABLE_SENSE_CHECK` precedent). Candidate rows persist regardless
of the flag, so flipping it back is a clean rollback with no data loss.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.ocr import OcrPort
from app.application.ports.vision import VisionPort
from app.application.purchase.ingest_candidate import (
    IngestPurchaseCandidate,
    ObjectKeySigner,
    sign_object_key,
)
from app.bootstrap.container import link_parser, ocr_service, vision_service
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.importers.router import RoutedLinkParser
from app.infrastructure.persistence.models.purchase_decision import PurchaseCandidate
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.purchase_decision_repo import (
    SqlAlchemyPurchaseCandidateRepository,
)
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.purchase_candidates import (
    CreatePurchaseCandidateRequest,
    ParsedItemModel,
    PurchaseCandidateResponse,
)


def _gate() -> None:
    if not get_settings().ENABLE_V3_PURCHASE_DECISION:
        raise FriendlyException(
            error_codes.FORBIDDEN,
            "V3 purchase decision is not enabled",
            status_code=503,
        )


# Router-level rather than a first line in each handler (the `ENABLE_SENSE_CHECK`
# style): FastAPI solves dependencies before it validates the request body, so a
# flag-off request gets 503 without its body being inspected. A 422 there would
# leak the request schema of a surface that is supposed to not exist yet.
router = APIRouter(
    prefix="/v1/purchase-candidates",
    tags=["purchase-candidates"],
    dependencies=[Depends(_gate)],
)


def get_vision_service() -> VisionPort:
    return vision_service()


def get_ocr_service() -> OcrPort:
    return ocr_service()


def get_link_parser() -> RoutedLinkParser:
    return link_parser()


def get_object_key_signer() -> ObjectKeySigner:
    return sign_object_key


def _to_response(candidate: PurchaseCandidate) -> PurchaseCandidateResponse:
    raw = candidate.parsed_item
    return PurchaseCandidateResponse(
        id=candidate.id,
        status=candidate.status,
        parsed_item=ParsedItemModel.model_validate(raw) if isinstance(raw, dict) else None,
    )


# The error states are declared so a generated client can see them: without
# this OpenAPI advertises only 201/422 and FE would have to discover the
# flag-off 503 (contract §2 rollback rule 1) at runtime. Descriptions only — the
# body is the service-wide `{error_code, message}` envelope, which no router in
# this repo re-declares per route.
_CREATE_ERRORS: dict[int | str, dict[str, str]] = {
    400: {"description": "`source_url` failed the SSRF safety check."},
    404: {"description": "`source_asset_id` is not an object key under the caller's own prefix."},
    503: {"description": "`ENABLE_V3_PURCHASE_DECISION` is off."},
}
_READ_ERRORS: dict[int | str, dict[str, str]] = {
    404: {"description": "No such candidate for this user (a foreign candidate answers 404, not 403)."},
    503: {"description": "`ENABLE_V3_PURCHASE_DECISION` is off."},
}


@router.post(
    "",
    response_model=PurchaseCandidateResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_CREATE_ERRORS,
)
async def create_purchase_candidate(
    payload: CreatePurchaseCandidateRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: Annotated[User, Depends(get_current_user)],
    vision: Annotated[VisionPort, Depends(get_vision_service)],
    ocr: Annotated[OcrPort, Depends(get_ocr_service)],
    links: Annotated[RoutedLinkParser, Depends(get_link_parser)],
    signer: Annotated[ObjectKeySigner, Depends(get_object_key_signer)],
) -> PurchaseCandidateResponse:
    candidate = await IngestPurchaseCandidate(
        SqlAlchemyPurchaseCandidateRepository(session),
        vision,
        ocr,
        links,
        signer=signer,
    ).execute(
        current_user.id,
        source_type=payload.source_type,
        source_url=payload.source_url,
        # Contract §5.2 calls this `source_asset_id`; the durable column is the
        # storage object key it actually carries (see the request schema).
        source_object_key=payload.source_asset_id,
    )
    return _to_response(candidate)


@router.get("/{candidate_id}", response_model=PurchaseCandidateResponse, responses=_READ_ERRORS)
async def get_purchase_candidate(
    candidate_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PurchaseCandidateResponse:
    candidate = await SqlAlchemyPurchaseCandidateRepository(session).get_for_user(
        candidate_id, current_user.id
    )
    if candidate is None:
        # Someone else's candidate is NOT_FOUND, not FORBIDDEN (contract §3).
        raise FriendlyException(error_codes.NOT_FOUND, "Purchase candidate not found", status_code=404)
    return _to_response(candidate)
