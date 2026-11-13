# V3 telemetry transport — FE #157 / V3-023 remediation

## What is implemented

The seven FE-owned events go through the existing `setAnalyticsDispatcher` port
registered before App renders, not a vendor SDK. The boundary accepts only the
frozen V3 envelope and per-event fields, including when callers bypass
`trackV3` and use legacy `track`. Legacy untyped events are not exported.

An authenticated same-origin `POST /v1/analytics/events` revalidates the whole
batch and checks the event user against the authenticated user. It rejects
client attempts to emit any of the four BE-owned events. The backend's existing
emitter and this relay share one delivery port, registered in BOTH FastAPI and
the unified ARQ worker lifecycle. This includes background preview outcomes.
No product decision/generation result is fabricated by the transport. Valid BE
event properties are no longer copied to general application logs, so disabling
the delivery port does not leave a second behavioral-data sink. Invalid-event
diagnostics contain no payload or caller-controlled values.

Real login uses the JWT subject for the client envelope; decoding is not auth.
The backend still verifies the token/session. A malformed token never falls
back to the development ID. Production without a token emits nothing. Web is
excluded because the frozen platform vocabulary is mobile-only; it is not
silently reported as iOS. Tokens, refresh credentials and device IDs are never
event properties. The delivery fetch never uses the app's token-refresh/toast
interceptor. Logout synchronously discards buffered events and aborts in-flight
requests; token/account changes also discard rather than replay prior data.
Async screen callbacks remain bound to their originating session and are rejected
after rotation, rather than being relabeled as the new user.

## Delivery contract (not a new event taxonomy)

`POST` JSON is `{ "events": [{ "event_id": "uuid", "event_name": "v3_...", "properties": { ... } }] }`.
`properties` is the unchanged V3 event contract. `event_id` is transport
identity only. The HTTP exporter adds `X-Aidrobe-Environment: dev|test|prod`
and the approved collector credential in request headers, not event properties.
The FE never sees a collector endpoint or credential.

A collector MUST use a unique key `(environment, event_id)`, accept replays
idempotently, and acknowledge only AFTER durable storage. Every retry of a
record keeps its ID and original `occurred_at`. Across independently generated
business events, flow-based metric definitions remain authoritative; transport
idempotency is not a claim of exactly-once business emission. Keep source-user
and environment partitioning in every query. Do not sample individual event
names: this implementation uses 100% of valid emitted events while enabled,
subject to the explicit best-effort loss bounds below.

The FE holds at most 32 pending records plus 8 in flight, batches at most 8,
expires queued data after 60 seconds, and tries a batch at most three times.
Each HTTP attempt has a 2.5-second cancellation deadline; retries wait 250/500 ms.
No disk spool is used. Backend memory has at most 128 pending plus 8 in flight,
the same TTL/batch/retry limits and a 2.5-second attempt deadline. Network/5xx/429
retry; other 4xx drop. Backpressure/queue absence responds 503 without affecting
business requests. Request bodies are capped at 16 KiB and validation errors
contain no rejected values. The exporter does not read collector response bodies,
follow redirects, or inherit ambient proxy configuration.

**The relay's 202 means memory enqueue, NOT durability.** Process death, shutdown,
offline timeout, queue overflow, collection kill and auth rotation can lose
records. Metrics must be treated as incomplete until delivery/drop monitoring,
retention and completeness are validated in the approved deployment. The current
local pump counters are diagnostics, not a durable monitoring system. These
bounds must not be described as an offline delivery guarantee.

## Default-off configuration and kill

FE: `ENABLE_V3_ANALYTICS=false` by default, independent of all V3 product flags.
BE: same default. When enabling in an approved environment, configure
`V3_ANALYTICS_COLLECTOR_URL` and, where required,
`V3_ANALYTICS_COLLECTOR_TOKEN` (server only). Production collector URLs must be
HTTPS, with no embedded credentials/query/fragment. Configure distinct approved
collector partitions/endpoints/credentials for development, staging and production;
map staging to the existing BE `test` environment, never to prod.
No endpoint, key or deployment setting has been provisioned by this change.

The FE runtime `setV3AnalyticsEnabled(false)` immediately cancels/discards;
the disabled API replies with `X-Aidrobe-Telemetry-Disabled: 1`, which makes
that client stop independently of product flags. Backend pump checks the gate
before enqueue/retry; shutdown cancels in-flight work. Operational disablement
must cover BOTH API and worker processes (settings reload/restart is required
for environment changes). Revoke the collector credential/disable collector
acceptance for an immediate server-side collection stop while rolling processes.
Already delivered records cannot be unsent; approved deletion is separate.

## Reproducible, synthetic query proof

From the BE worktree, use its existing lightweight Python environment:

`python scripts/verify_v3_telemetry_join.py --fe-dir <paired FE worktree> --out <new evidence directory>`

The runner executes the actual FE release dispatcher with mocked native/auth/network
I/O, posts its batches through the actual ASGI relay, emits actual BE event-port
records with synthetic outcomes, and executes the real HTTP exporter against an
injected HTTPX collector. That fixture durably stores rows in SQLite and queries
three paths: decision-only; preview shown plus feedback; failed preview with a
retained decision action. It replays a batch and asserts deduplication. It writes
`query-proof.json` and `synthetic-events.sqlite3` without overwriting old evidence.
Only synthetic IDs are used; no real network, user photo, paid provider or native
build runs. This is transport/correlation evidence, NOT end-to-end business/UI
validation or provider-quality evidence.

The report stamps paired Git heads and mirrored artifact hashes outside event
properties. CI is still responsible for the full suites. The event registry
JSON remains byte-identical; OpenAPI and the truthful readiness detail are
updated in both repos.

## Still a human release gate

`telemetry_sink` remains **unmet**. Before quantitative seed rollout, approve
the collector/processor and region, retention duration, backup retention,
least-privilege readers, deletion/export procedure for UUID-linked records,
per-environment access/credentials, monitoring of drops/delivery and metric
completeness. Verify actual paired build/source/flags, mobile identity/flow
behavior and API+worker delivery into that collector. None of these approvals
is inferred from the synthetic test. All production flags remain off; qualitative
decision-only research and paid visual beta have their own existing approvals.
