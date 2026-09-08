"""Synthetic, container-free FE/BE transport proof; never contacts a real collector.

The shipped FE release dispatcher produces the batches. The actual BE relay,
emitter, delivery pump and HTTP adapter write via a MockTransport into SQLite.
This proves transport/correlation, NOT native UI, business generation or rollout.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.common.analytics_delivery import set_analytics_sink  # noqa: E402
from app.common.analytics_emit import emit_v3_event  # noqa: E402
from app.config.settings import Settings  # noqa: E402
from app.infrastructure.analytics_transport import AnalyticsPump, HttpAnalyticsSink  # noqa: E402
from app.presentation.api.v1.deps import get_current_user  # noqa: E402
from app.presentation.api.v1.routers import analytics  # noqa: E402

USER = uuid.UUID("00000000-0000-4000-8000-000000000157")
FLOWS = [f"00000000-0000-4000-8000-000000000{number}" for number in (161, 162, 163)]


def source_head(repo: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


async def verify(fe: Path, output: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    database = output / "synthetic-events.sqlite3"
    if database.exists():
        raise RuntimeError("Refusing to overwrite prior evidence; use a new output directory")
    conn = sqlite3.connect(database)
    conn.execute(
        "CREATE TABLE events (environment TEXT, event_id TEXT, flow_id TEXT, "
        "occurred_at TEXT, name TEXT, properties TEXT, PRIMARY KEY(environment,event_id))"
    )
    cfg = Settings(
        _env_file=None,
        APP_ENV="test",
        ENABLE_V3_ANALYTICS=True,
        V3_ANALYTICS_COLLECTOR_URL="https://synthetic-collector.invalid",
    )

    async def collect(request: httpx.Request) -> httpx.Response:
        environment = request.headers["X-Aidrobe-Environment"]
        assert environment == "test"
        for event in json.loads(request.content)["events"]:
            props = event["properties"]
            conn.execute(
                "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)",
                (
                    environment,
                    event["event_id"],
                    props.get("flow_id"),
                    props["occurred_at"],
                    event["event_name"],
                    json.dumps(props, sort_keys=True),
                ),
            )
        conn.commit()  # The fixture collector ACKs only after durable commit.
        return httpx.Response(202)

    exporter = HttpAnalyticsSink(cfg, transport=httpx.MockTransport(collect))
    pump = AnalyticsPump(exporter.send, enabled=lambda: cfg.ENABLE_V3_ANALYTICS)
    set_analytics_sink(pump.enqueue)
    original_settings = analytics.get_settings
    analytics.get_settings = lambda: cfg
    app = FastAPI()
    app.include_router(analytics.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=USER)
    first_batches = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://synthetic-api.invalid"
        ) as client:
            for phase in ("start", "viewed", "terminal"):
                result = subprocess.run(
                    ["node", "scripts/v3TelemetryFixture.ts", phase],
                    cwd=fe,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=20,
                    check=True,
                )
                batches = json.loads(result.stdout)
                if first_batches is None:
                    first_batches = batches
                for batch in batches:
                    response = await client.post("/v1/analytics/events", json=batch)
                    assert response.status_code == 202, response.text
                await pump.flush()
                if phase == "start":
                    for index, flow in enumerate(FLOWS):
                        assert emit_v3_event(
                            "v3_decision_completed",
                            {
                                "decision": "worth_adding",
                                "uncertainty": "low",
                                "visual_need": "none" if index == 0 else "helpful",
                                "parse_status": "parsed",
                            },
                            user_id=USER,
                            flow_id=flow,
                        )
                if phase == "viewed":
                    for index, flow in enumerate(FLOWS[1:]):
                        assert emit_v3_event(
                            "v3_preview_outcome",
                            {
                                "generation_status": "ready" if index == 0 else "failed",
                                "cache_hit": False,
                                "retry_count": 0,
                            },
                            user_id=USER,
                            flow_id=flow,
                        )
                await pump.flush()
            before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            for batch in first_batches or []:
                assert (await client.post("/v1/analytics/events", json=batch)).status_code == 202
            await pump.flush()
            assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
        flows = []
        for index, flow in enumerate(FLOWS):
            rows = conn.execute(
                "SELECT name,properties FROM events WHERE environment=? AND flow_id=? ORDER BY occurred_at,name",
                ("test", flow),
            ).fetchall()
            events = [{"name": name, "properties": json.loads(props)} for name, props in rows]
            names = [event["name"] for event in events]
            for required in (
                "v3_decision_started",
                "v3_decision_completed",
                "v3_decision_viewed",
                "v3_decision_action",
            ):
                assert names.count(required) == 1, (flow, names)
            assert (
                names.index("v3_decision_started")
                < names.index("v3_decision_completed")
                < names.index("v3_decision_viewed")
            )
            if index:
                assert (
                    names.index("v3_preview_requested")
                    < names.index("v3_preview_outcome")
                    < names.index("v3_preview_viewed")
                    < names.index("v3_decision_action")
                )
                viewed = next(event["properties"] for event in events if event["name"] == "v3_preview_viewed")
                assert viewed["result"] == ("shown" if index == 1 else "fallback")
            else:
                assert not any(name.startswith("v3_preview_") for name in names)
            if index == 1:
                assert names.count("v3_preview_feedback") == 1
            for event in events:
                assert event["properties"]["user_id"] == str(USER)
                assert event["properties"]["platform"] == (
                    "backend" if event["name"] in {"v3_decision_completed", "v3_preview_outcome"} else "ios"
                )
            flows.append(
                {
                    "flow_id": flow,
                    "path": ["decision_only", "preview_ready", "preview_fallback"][index],
                    "events": events,
                }
            )
        hashes = {}
        for relative in ("docs/openapi.json", "docs/v3-analytics-events.json", "docs/v3-rollout-dashboard.json"):
            left = (fe / relative).read_text(encoding="utf-8").replace("\r\n", "\n")
            right = (ROOT / relative).read_text(encoding="utf-8").replace("\r\n", "\n")
            assert left == right, f"Paired artifact mismatch: {relative}"
            hashes[relative] = hashlib.sha256(left.encode()).hexdigest()
        report = {
            "fixture_only": True,
            "real_network_calls": 0,
            "real_users": 0,
            "provider": "synthetic_outcome_not_a_provider_run",
            "database": database.name,
            "fe_tree_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=fe)),
            "be_tree_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
            "fe_head": source_head(fe),
            "be_head": source_head(ROOT),
            "artifact_sha256_normalized": hashes,
            "unique_events": before,
            "replay_deduplication_verified": True,
            "flows": flows,
            "telemetry_sink_readiness_met": False,
            "limitations": (
                "Mock native runtime and auth, ASGI API, injected BE outcomes, mock HTTP collector; "
                "no device, business-generation or production retention evidence."
            ),
        }
        (output / "query-proof.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return report
    finally:
        analytics.get_settings = original_settings
        set_analytics_sink(None)
        await pump.close()
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fe-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    report = asyncio.run(verify(args.fe_dir.resolve(), args.out.resolve()))
    print(
        json.dumps(
            {
                "flows_verified": len(report["flows"]),
                "unique_events": report["unique_events"],
                "replay_deduplication_verified": True,
                "fixture_only": True,
            }
        )
    )
