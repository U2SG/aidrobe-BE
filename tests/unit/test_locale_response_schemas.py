from __future__ import annotations

import uuid

from app.common.i18n import make_message
from app.presentation.api.v1.schemas.ask import AskResponse
from app.presentation.api.v1.schemas.shopping import SenseCheckResponse


def test_sense_check_response_serializes_structured_display_reason() -> None:
    message = make_message(
        "sense_check.consider.some",
        "你已经有一些 top，它还能和 1 件现有单品搭配。",
        {"category": "top", "count": 1},
    )
    response = SenseCheckResponse(
        verdict="consider",
        reason=message.fallback_text,
        display_reason=message,
        similar_item_count=0,
        similar_items=[],
        wardrobe_gap=[],
        outfit_potential=1,
    )

    assert response.model_dump(mode="json")["display_reason"] == {
        "key": "sense_check.consider.some",
        "params": {"category": "top", "count": 1},
        "fallback_text": message.fallback_text,
    }


def test_ask_response_serializes_locale_and_context_flag() -> None:
    response = AskResponse(
        ask_session_id=uuid.uuid4(),
        conversation_id=None,
        answer="Use the white shirt.",
        item_refs=[],
        item_ref_snapshots=[],
        has_wardrobe_context=False,
        locale="en-US",
        tokens_used=12,
        deltas=None,
    )

    dumped = response.model_dump(mode="json")
    assert dumped["has_wardrobe_context"] is False
    assert dumped["locale"] == "en-US"
