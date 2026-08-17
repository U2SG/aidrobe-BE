"""V3 purchase-decision flag gating and scope discipline.

V3-003 shipped the flag with no router, so this file could only assert that no
route existed — which would have passed with the flag on too. V3-004 adds the
first two routes, so the gate is now testable for real, and the check is written
generically: **every** registered route under the V3 purchase-decision prefixes
must answer 503 while the flag is off. V3-007's decision route inherited that
assertion without changing it; V3-008 adds the last one the same way.

Endpoint-specific behaviour lives in tests/api/test_v3_purchase_candidates.py
and tests/api/test_v3_purchase_decision_endpoint.py.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.infrastructure.persistence.session import get_db_session
from app.main import create_app
from app.presentation.api.v1.routers import purchase_candidates, visual_preview
from app.presentation.middlewares.exception_handler import setup_exception_handlers

V3_DECISION_PREFIXES = ("/v1/purchase-candidates", "/v1/wardrobe-decisions")

# Surfaces the contract freezes but no shipped feature owns yet. Listed so that
# adding one without its own feature is visible here rather than silent.
# V3-019 shipped §6.1's `POST …/visual-preview` (gated by ENABLE_V3_STATIC_TRYON,
# not the decision flag — the generic 503 test below covers it because BOTH
# flags are off in the Settings it builds). §6.1's feedback endpoint is not
# under these prefixes and is owned by V3-020.
NOT_YET_IMPLEMENTED: tuple[str, ...] = ()


def _v3_routes() -> list[APIRoute]:
    return [
        route
        for route in create_app().routes
        if isinstance(route, APIRoute) and route.path.startswith(V3_DECISION_PREFIXES)
    ]


def test_flag_defaults_off() -> None:
    assert Settings().ENABLE_V3_PURCHASE_DECISION is False


def test_v3_candidate_routes_are_registered() -> None:
    """Guards the generic gate test below against silently testing nothing."""
    paths = {route.path for route in _v3_routes()}

    assert "/v1/purchase-candidates" in paths
    assert "/v1/purchase-candidates/{candidate_id}" in paths
    # V3-007 — the evaluation itself. Acceptance 4 ("flag off cleanly hides the
    # evaluation") is the generic 503 test below applied to this path.
    assert "/v1/purchase-candidates/{candidate_id}/decision" in paths
    # V3-008 — the snapshot replay surface, the last of the four §5.1 routes.
    assert "/v1/wardrobe-decisions/{decision_id}" in paths
    # V3-019 — §6.1's preview create surface, so the generic gate test below
    # cannot silently stop covering it.
    assert "/v1/wardrobe-decisions/{decision_id}/visual-preview" in paths


def test_later_features_have_not_been_pulled_forward() -> None:
    paths = {route.path for route in _v3_routes()}

    assert not paths.intersection(NOT_YET_IMPLEMENTED)


@pytest.mark.parametrize("method", ["POST", "GET"])
async def test_every_v3_decision_route_is_503_when_the_flag_is_off(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    # Both V3 flags off — the preview route (V3-019) gates on
    # ENABLE_V3_STATIC_TRYON, which Settings() also defaults to False.
    monkeypatch.setattr(
        purchase_candidates,
        "get_settings",
        lambda: Settings(APP_ENV="dev", ENABLE_V3_PURCHASE_DECISION=False),
    )
    monkeypatch.setattr(
        visual_preview,
        "get_settings",
        lambda: Settings(APP_ENV="dev", ENABLE_V3_STATIC_TRYON=False),
    )
    app = FastAPI()
    setup_exception_handlers(app)

    async def _db() -> AsyncSession:
        return session

    session = db_session
    app.dependency_overrides[get_db_session] = _db
    app.include_router(purchase_candidates.router)
    app.include_router(purchase_candidates.wardrobe_decisions_router)
    app.include_router(visual_preview.router)

    checked = 0
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        for route in _v3_routes():
            if method not in route.methods:
                continue
            # Substitute a concrete id for any path parameter.
            path = route.path.replace("{candidate_id}", str(uuid.uuid4())).replace(
                "{decision_id}", str(uuid.uuid4())
            )
            resp = await client.request(
                method,
                path,
                headers={"X-User-Id": str(uuid.uuid4())},
                json={} if method == "POST" else None,
            )
            # 503 must win over request validation (422): the surface does not
            # exist while the flag is off, so the body is never even inspected.
            assert resp.status_code == 503, f"{method} {path} -> {resp.status_code}"
            checked += 1

    assert checked >= 1
