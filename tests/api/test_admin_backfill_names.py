"""Admin backfill of legacy localized item names."""

from __future__ import annotations

import json
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.llm import LlmAnswer
from app.bootstrap.container import llm_service
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import admin as admin_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


class _FakeNamingLlm:
    async def polish_reason(self, prompt: str) -> str:
        raise NotImplementedError

    async def ask(self, system_prompt: str, question: str, *, json_mode: bool = False) -> LlmAnswer:
        entries = json.loads(question)
        names = [
            # Echo idx; zh derived from the title so assertions can tie the
            # translation back to the right row.
            {"idx": e["idx"], "zh": f"中文{e['idx']}", "en": e["title"]}
            for e in entries
        ]
        return LlmAnswer(text=json.dumps({"names": names}, ensure_ascii=False), tokens_used=10)


def _build_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db() -> AsyncSession:
        return session

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_settings] = lambda: Settings(APP_ENV="dev")
    app.dependency_overrides[llm_service] = lambda: _FakeNamingLlm()
    app.include_router(admin_router.router)
    return app


async def test_backfill_fills_ai_items_and_skips_user_named(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    legacy = WardrobeItem(
        user_id=user_id,
        title="White Polo Shirt",
        category="top",
        color_main="white",
        status="wearable",
        name_source="ai",
    )
    user_named = WardrobeItem(
        user_id=user_id,
        title="My Fancy Shirt",
        category="top",
        color_main="white",
        status="wearable",
        name_source="user",
    )
    db_session.add_all([legacy, user_named])
    await db_session.flush()

    app = _build_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/admin/backfill-item-names", headers={"X-User-Id": str(user_id)}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"scanned": 1, "updated": 1, "remaining": 0}
    await db_session.refresh(legacy)
    assert legacy.localized_names == {"zh-CN": "中文0", "en-US": "White Polo Shirt"}
    assert legacy.canonical_name == "White Polo Shirt"
    # User-renamed rows are never rewritten.
    await db_session.refresh(user_named)
    assert user_named.localized_names is None
