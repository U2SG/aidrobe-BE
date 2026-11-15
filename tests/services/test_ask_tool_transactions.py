"""Cancellation of a tool query must leave the answer's transaction usable."""

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.application.ask import answer_question as ask_uc
from app.config.settings import Settings
from app.infrastructure.llm.qwen_llm import QwenLlm
from app.infrastructure.persistence.models.ask_session import AskSession
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem


async def test_tool_database_timeout_does_not_cancel_the_request_transaction(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    db_session.add(WardrobeItem(user_id=user_id, title="Shirt", category="top", color_main="white"))
    await db_session.commit()
    assert isinstance(db_session.bind, AsyncConnection)
    await db_session.bind.commit()
    parent_pid = await db_session.scalar(text("SELECT pg_backend_pid()"))
    async with AsyncSession(bind=db_session.bind.engine) as warmup:
        await warmup.execute(text("SELECT 1"))

    settings = Settings(
        APP_ENV="dev", ENABLE_ASK_TOOLS=True, ENABLE_ASK_STRUCTURED_OUTPUT=True,
        QWEN_API_KEY="test", ASK_TOOL_TOTAL_TIMEOUT_SECONDS=0.3,
    )
    monkeypatch.setattr(ask_uc, "get_settings", lambda: settings)
    real_scalars = AsyncSession.scalars
    query_cancelled = False
    tool_pid = None

    async def delayed_scalars(session: AsyncSession, statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal query_cancelled, tool_pid
        if session is not db_session:
            tool_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            try:
                await session.execute(text("SELECT pg_sleep(5)"))
            except asyncio.CancelledError:
                query_cancelled = True
                raise
        return await real_scalars(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "scalars", delayed_scalars)
    real_client = httpx.AsyncClient

    async def provider(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("tools"):
            message = {"role": "assistant", "content": "", "tool_calls": [{
                "id": "search", "type": "function",
                "function": {"name": "search_wardrobe", "arguments": '{"category":"top"}'},
            }]}
        else:
            message = {"role": "assistant", "content": json.dumps({
                "answer": "还需要确认场景。", "reply_kind": "general", "referenced_items": [],
            })}
        return httpx.Response(200, request=request, json={
            "choices": [{"message": message}], "usage": {"total_tokens": 3},
        })

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: real_client(transport=httpx.MockTransport(provider)))
    result = await ask_uc.answer_question(
        db_session, user_id, question="怎么搭这件衣服？", context=None, llm=QwenLlm(settings),
    )
    assert query_cancelled and tool_pid != parent_pid
    assert await db_session.scalar(text("SELECT pg_backend_pid()")) == parent_pid
    assert result.answer == "还需要确认场景。" and result.item_refs == []
    assert await db_session.get(AskSession, result.ask_session_id) is not None
