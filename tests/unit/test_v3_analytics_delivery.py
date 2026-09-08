"""Container-free tests of the actual relay/port/exporter (FE #157)."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.common import analytics_delivery as delivery
from app.common.analytics_emit import emit_v3_event
from app.common.analytics_events import FORBIDDEN_FIELD_NAMES
from app.config.settings import Settings
from app.infrastructure.analytics_transport import AnalyticsPump, HttpAnalyticsSink
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.routers import analytics
from app.presentation.middlewares.exception_handler import setup_exception_handlers

USER = uuid.UUID("00000000-0000-4000-8000-000000000157")
FLOW = "00000000-0000-4000-8000-000000000161"


def record():
    return {
        "event_id": str(uuid.uuid4()),
        "event_name": "v3_decision_started",
        "properties": {
            "schema_version": 1,
            "occurred_at": datetime.now(UTC).isoformat(),
            "platform": "ios",
            "user_id": str(USER),
            "flow_id": FLOW,
            "source_type": "share_link",
        },
    }


@pytest.fixture
def relay(monkeypatch):
    cfg = Settings(
        _env_file=None,
        APP_ENV="test",
        ENABLE_V3_ANALYTICS=True,
        V3_ANALYTICS_COLLECTOR_URL="https://collector.fixture.invalid",
    )
    monkeypatch.setattr(analytics, "get_settings", lambda: cfg)
    captured = []
    monkeypatch.setattr(delivery, "_sink", lambda records: captured.extend(records) or True)
    app = FastAPI()
    setup_exception_handlers(app)
    app.include_router(analytics.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=USER)
    return app, cfg, captured


def test_record_snapshot_identity_and_ownership():
    raw = record()
    accepted = delivery.validate_record(raw, owner="fe", user_id=USER)
    raw["properties"]["source_type"] = "PRIVATE"
    assert accepted["properties"]["source_type"] == "share_link"
    with pytest.raises(ValueError, match="^Invalid analytics record$"):
        delivery.validate_record(accepted, owner="be", user_id=USER)
    with pytest.raises(ValueError):
        delivery.validate_record(accepted, owner="fe", user_id=uuid.uuid4())


@pytest.mark.parametrize("key", [*sorted(FORBIDDEN_FIELD_NAMES), "unknown", "constructor", "__proto__"])
def test_all_sensitive_and_unknown_fields_are_rejected_without_echo(key):
    raw = record()
    raw["properties"][key] = "PRIVATE_VALUE_DO_NOT_LOG"
    with pytest.raises(ValueError, match="^Invalid analytics record$") as exc:
        delivery.validate_record(raw, owner="fe", user_id=USER)
    assert "PRIVATE" not in str(exc.value)
    assert key not in str(exc.value)


@pytest.mark.parametrize(
    "patch",
    [
        {"platform": "web"},
        {"platform": "backend"},
        {"schema_version": True},
        {"user_id": "PRIVATE"},
        {"flow_id": "PRIVATE"},
        {"occurred_at": "PRIVATE"},
        {"occurred_at": "2026-02-31T00:00:00Z"},
        {"source_type": {"nested": "PRIVATE"}},
    ],
)
def test_envelope_and_nested_payload_values_are_rejected(patch):
    raw = record()
    raw["properties"].update(patch)
    with pytest.raises(ValueError):
        delivery.validate_record(raw, owner="fe", user_id=USER)


async def test_authenticated_atomic_relay_and_private_error_path(relay):
    app, cfg, captured = relay
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://api.fixture.invalid") as client:
        assert (await client.post("/v1/analytics/events", json={"events": [record()]})).status_code == 202
        assert len(captured) == 1
        bad = record()
        bad["properties"]["email"] = "PRIVATE_VALUE_DO_NOT_LOG"
        response = await client.post("/v1/analytics/events", json={"events": [record(), bad]})
        assert response.status_code == 422 and "PRIVATE" not in response.text
        assert len(captured) == 1  # Whole batch validated before enqueue.
        bad = record()
        bad["properties"]["user_id"] = str(uuid.uuid4())
        assert (await client.post("/v1/analytics/events", json={"events": [bad]})).status_code == 422
        bad = record()
        bad["event_name"] = "v3_decision_completed"
        assert (await client.post("/v1/analytics/events", json={"events": [bad]})).status_code == 422
        assert (await client.post("/v1/analytics/events", content=b"x" * 16385)).status_code == 413
        assert (await client.post("/v1/analytics/events", content=b"[" * 2000 + b"]" * 2000)).status_code == 422
        assert (await client.post("/v1/analytics/events", json={"events": [record()] * 9})).status_code == 422
        cfg.ENABLE_V3_ANALYTICS = False
        response = await client.post("/v1/analytics/events", json={"events": [record()]})
        assert response.status_code == 503 and response.headers["X-Aidrobe-Telemetry-Disabled"] == "1"
        assert len(captured) == 1


async def test_disabled_gate_precedes_auth(relay):
    app, cfg, _ = relay
    cfg.ENABLE_V3_ANALYTICS = False

    def must_not_authenticate():
        raise AssertionError("Disabled telemetry must not read identity")

    app.dependency_overrides[get_current_user] = must_not_authenticate
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://api.fixture.invalid") as client:
        assert (await client.post("/v1/analytics/events", content=b"PRIVATE")).status_code == 503


def test_default_off_and_collector_configuration():
    assert not Settings(_env_file=None).ENABLE_V3_ANALYTICS
    with pytest.raises(ValueError):
        Settings(_env_file=None, ENABLE_V3_ANALYTICS=True)
    for url in ["http://collector.invalid", "https://u:p@collector.invalid", "https://collector.invalid?token=x"]:
        with pytest.raises(ValueError):
            Settings(_env_file=None, APP_ENV="prod", ENABLE_V3_ANALYTICS=True, V3_ANALYTICS_COLLECTOR_URL=url)


async def test_pump_bounded_retry_stable_delivery_and_kill():
    attempts = []

    async def send(batch):
        attempts.append(batch)
        return 503 if len(attempts) < 3 else 202

    async def no_delay(_seconds):
        pass

    pump = AnalyticsPump(send, enabled=lambda: True, sleep=no_delay)
    event = delivery.validate_record(record(), owner="fe", user_id=USER)
    assert pump.enqueue([event])
    await pump.flush()
    assert len(attempts) == 3 and pump.delivered == 1
    assert all(batch[0]["event_id"] == event["event_id"] for batch in attempts)
    assert all(batch[0]["properties"]["occurred_at"] == event["properties"]["occurred_at"] for batch in attempts)
    await pump.close()
    assert not pump.enqueue([event])
    clock = [0.0]
    calls = []

    async def accepted(batch):
        calls.append(batch)
        return 202

    bounded = AnalyticsPump(accepted, enabled=lambda: True, now=lambda: clock[0])
    assert bounded.enqueue([event] * 128)
    assert not bounded.enqueue([event])
    clock[0] = 61
    await bounded.flush()
    assert calls == [] and bounded.dropped == 128
    await bounded.close()
    enabled = [True]
    killed = AnalyticsPump(accepted, enabled=lambda: enabled[0])
    assert killed.enqueue([event])
    enabled[0] = False
    await killed.flush()
    assert calls == [] and killed.dropped == 1
    await killed.close()


async def test_pump_close_cancels_inflight():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def send(_batch):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return 202

    pump = AnalyticsPump(send, enabled=lambda: True)
    assert pump.enqueue([delivery.validate_record(record(), owner="fe", user_id=USER)])
    await started.wait()
    await pump.close()
    assert cancelled.is_set()


async def test_exporter_uses_server_environment_and_no_response_body():
    received = []

    async def collector(request):
        received.append(request)
        return httpx.Response(302, headers={"Location": "https://unapproved.invalid"})

    cfg = Settings(
        _env_file=None,
        APP_ENV="test",
        ENABLE_V3_ANALYTICS=True,
        V3_ANALYTICS_COLLECTOR_URL="https://collector.fixture.invalid",
        V3_ANALYTICS_COLLECTOR_TOKEN="fixture-only",
    )
    sink = HttpAnalyticsSink(cfg, transport=httpx.MockTransport(collector))
    event = delivery.validate_record(record(), owner="fe", user_id=USER)
    assert await sink.send([event]) == 302
    assert len(received) == 1 and received[0].headers["X-Aidrobe-Environment"] == "test"
    assert received[0].headers["Authorization"] == "Bearer fixture-only"
    assert json.loads(received[0].content) == {"events": [event]}
    assert "fixture-only" not in str(event)


async def test_api_and_worker_lifecycle_register_real_backend_emitter(monkeypatch):
    from app.bootstrap import analytics as bootstrap

    cfg = Settings(
        _env_file=None,
        APP_ENV="test",
        ENABLE_V3_ANALYTICS=True,
        V3_ANALYTICS_COLLECTOR_URL="https://collector.fixture.invalid",
    )
    monkeypatch.setattr(bootstrap, "get_settings", lambda: cfg)
    captured = []

    async def send(_self, records):
        captured.extend(records)
        return 202

    monkeypatch.setattr(HttpAnalyticsSink, "send", send)
    for start, stop in [
        (bootstrap.start_analytics, bootstrap.stop_analytics),
        (lambda: bootstrap.analytics_worker_startup({}), lambda: bootstrap.analytics_worker_shutdown({})),
    ]:
        await start()
        assert emit_v3_event(
            "v3_decision_completed",
            {
                "decision": "worth_adding",
                "uncertainty": "low",
                "visual_need": "none",
                "parse_status": "parsed",
            },
            user_id=USER,
            flow_id=FLOW,
        )
        await bootstrap._pump.flush()
        await stop()
    assert len(captured) == 2 and all(row["properties"]["platform"] == "backend" for row in captured)
    assert not delivery.publish_records([])


def test_delivery_failure_cannot_fail_business_emission(monkeypatch):
    def broken(_records):
        raise RuntimeError("PRIVATE")

    monkeypatch.setattr(delivery, "_sink", broken)
    assert emit_v3_event("v3_avatar_deleted", {"purge_derived": True}, user_id=USER)
    assert not emit_v3_event("v3_decision_started", {"source_type": "share_link"}, user_id=USER, flow_id=FLOW)


def test_disabled_sink_does_not_fall_back_to_payload_logging(monkeypatch):
    from app.common import analytics_emit
    calls = []
    monkeypatch.setattr(delivery, "_sink", None)
    monkeypatch.setattr(analytics_emit, "logger", SimpleNamespace(
        info=lambda *args, **kw: calls.append((args, kw)),
        warning=lambda *args, **kw: calls.append((args, kw)),
    ))
    assert emit_v3_event("v3_avatar_deleted", {"purge_derived": True}, user_id=USER)
    assert calls == []
    assert not emit_v3_event("v3_avatar_deleted", {"email": "PRIVATE"}, user_id=USER)
    assert calls == [(("v3_analytics_event_invalid",), {})]


def test_broken_diagnostics_cannot_break_business_request(monkeypatch):
    from app.common import analytics_emit
    def broken(*_args, **_kw):
        raise RuntimeError("logger unavailable")
    monkeypatch.setattr(analytics_emit, "logger", SimpleNamespace(warning=broken))
    assert not emit_v3_event("v3_avatar_deleted", {"email": "PRIVATE"}, user_id=USER)
