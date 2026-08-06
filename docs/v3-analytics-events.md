# V3 Analytics and Evaluation Events (Frozen)

Feature: **V3-002**. Authority: `V3_SPEC.md` §18/§20 and the immutable
acceptance criteria in `feature_list.json` outrank this document;
`docs/v3-p0-contract.md` (V3-001) freezes the API shapes this contract
observes and, per its §8, analytics must not modify them.

This document and `docs/v3-analytics-events.json` are copied **byte-identically**
into aidrobe-FE. The machine-readable artifact is the single source of truth:

| Artifact | Role |
|---|---|
| `app/common/analytics_events.py` (BE) | Generator of record; `validate_v3_event` is the emit-time gate. |
| `docs/v3-analytics-events.json` (both repos) | The contract both sides are tested against. |
| `src/utils/v3AnalyticsEvents.ts` (FE) | Typed mirror + `trackV3` emit gate. |
| `tests/unit/test_v3_analytics_events.py` (BE), `scripts/v3AnalyticsEvents.test.ts` (FE) | Drift guards. |

**Scope of V3-002:** the contract, the two mirrors and their drift tests. No
event is emitted yet — emitters ship with the feature that owns the surface
(V3-008 decision, V3-013 avatar, V3-019/V3-020 preview). Choosing a sink
(PostHog, self-hosted, warehouse) is not part of P0; FE keeps the existing
`setAnalyticsDispatcher` seam and BE keeps its structured logger.

## 1. Envelope

Every event carries exactly these fields:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `schema_version` | integer ≥ 1 | yes | Bumped only for a breaking change to this contract. |
| `occurred_at` | timestamp | yes | Client or server clock at emit time; all latency is derived from these. |
| `platform` | `ios\|android\|backend` | yes | Which side emitted. |
| `user_id` | uuid | yes | Existing account id; the only identity in the contract. |
| `flow_id` | uuid | conditional | Per §3. |

## 2. Event catalogue

`owner` is the **only** side allowed to emit that event — a duplicate emit from
the other side would double-count every rate below.

| Event | Owner | Group | `flow_id` | Fields |
|---|---|---|---|---|
| `v3_decision_started` | fe | decision | required | `source_type` |
| `v3_decision_completed` | be | decision | required | `decision`, `uncertainty`, `visual_need`, `parse_status` |
| `v3_decision_viewed` | fe | decision | required | `decision` |
| `v3_decision_action` | fe | decision | required | `action` |
| `v3_avatar_prompt_shown` | fe | avatar | when_in_flow | `source_type`, `context` |
| `v3_avatar_consent` | be | avatar | when_in_flow | `accepted`, `source_type` |
| `v3_avatar_deleted` | be | avatar | not_applicable | `purge_derived` |
| `v3_preview_requested` | fe | preview | required | `concern`, `avatar_status` |
| `v3_preview_outcome` | be | preview | required | `generation_status`, `cache_hit`, `retry_count` |
| `v3_preview_viewed` | fe | preview | required | `result` |
| `v3_preview_feedback` | fe | preview | required | `verdict`, `aspects` (optional) |

Enum values are in the JSON artifact and are not repeated here, so there is
exactly one place to change them.

## 3. Flow correlation

A **flow** is one "should I buy this" attempt: from the moment the user submits
a candidate to the moment they act on (or abandon) the decision, including any
preview requested inside it.

- FE generates a UUIDv4 `flow_id` when the flow starts and keeps it until the
  flow ends.
- FE sends it on every request belonging to that flow as the existing
  **`X-Correlation-Id`** header. This is why analytics needs no request-body or
  response-shape change (v3-p0-contract §8): `CorrelationIdMiddleware` already
  accepts, validates and propagates that header, and already binds it to the
  structured logger.
- BE derives the envelope `flow_id` with `resolve_flow_id(correlation_id)`. A
  correlation id that is already a UUID passes through normalized; any other
  valid correlation id is mapped to a stable derived UUID, so the envelope's
  uuid type holds and a non-conforming client still produces groupable events
  instead of dropped ones.

`flow_id` rules per event:

- `required` — the event only exists inside a flow. Emitting without one is a
  contract violation (`validate_v3_event` raises).
- `when_in_flow` — the avatar prompt and consent happen in two different
  places: inside a preview request (part of a flow — the `flow_id` **must** be
  attached, otherwise kill-criterion 4 in §6 cannot be computed) and after an
  unrelated OOTD/import flow (no flow exists; omit it).
- `not_applicable` — a Profile action that is never part of a flow. Attaching a
  `flow_id` is rejected.

## 4. Decision value is separable from visual-generation usage

V3-002 acceptance 1. Events are tagged `decision`, `avatar` or `preview`, and
the split is load-bearing rather than cosmetic:

- With `ENABLE_V3_AVATAR` and `ENABLE_V3_STATIC_TRYON` off, the `decision`
  group still emits completely, so **decision completion, time-to-decision,
  action rate, existing-item-alternative rate and repeat use are all reported
  with zero visual events**. A test asserts those metrics reference only
  `decision`-group events.
- Preview metrics are deliberately expressed as *usefulness* ratios
  (`post_preview_decision_action_rate`, `fallback_decision_retention_rate`)
  and *cost* ratios (`generations_per_useful_decision`). There is no
  "generations per user" metric and no counter, streak or quota anywhere —
  V3_SPEC §21 removes generation-count gamification, and a dashboard that
  rewards generation volume would reintroduce it through the back door.
- `v3_preview_viewed.result=fallback` plus `fallback_decision_retention_rate`
  are how "a failed or hidden preview never erases the decision" (V3_SPEC §3.6)
  becomes measurable instead of merely asserted.

## 5. Privacy field review

V3-002 acceptance 2, and v3-p0-contract §7.

**Structural rule:** every field in this contract is a closed value space —
enum, boolean, bounded integer, or an envelope uuid/timestamp. Free text does
not exist in the contract, so there is no field a photo URL, prompt, caption or
body inference could travel in even by accident. Tests assert this over the
declared field set of every event, not merely at call time.

Explicitly **not** collected, and named in `forbidden_field_names` so a future
addition fails loudly rather than silently shipping:

| Category | Examples in the denylist | Why |
|---|---|---|
| Image material | `image_url`, `image_bytes`, `image_base64`, `photo_url`, `preview_url`, `signed_url`, `canonical_front_url` | Person photos are high-sensitivity; signed URLs are also short-lived credentials. |
| Body / appearance inference | `body_measurements`, `body_shape_estimate`, `height`, `weight`, `age_estimate`, `attractiveness_score` | V3_SPEC §21 forbids body scoring; no size or fit claim may exist anywhere. |
| Model internals | `prompt`, `prompt_text`, `prompt_version`, `provider`, `model_version`, `quality_signals` | Vendor detail stays internal (v3-p0-contract §6.4) and prompts can embed user content. |
| Free text | `comment`, `note`, `free_text`, `metadata` | An open value space defeats every guarantee above. |
| Extra identity | `email`, `phone`, `device_id`, `ip_address`, `source_url` | `user_id` is sufficient; `source_url` can identify a specific product page and person. |

Deliberate omissions worth stating, because their absence looks like a gap
until you see the reason:

- **No `duration_ms` on any event.** Latency is derived from `occurred_at`
  deltas joined on `flow_id`. A client-reported duration is unverifiable and
  would be the contract's only open numeric field.
- **No candidate id, item id or asset id.** Rates and ratios never need them,
  and they would join analytics back to wardrobe content.
- **No free-text feedback.** `v3_preview_feedback` is verdict + optional
  aspects; V3_SPEC's qualitative work (§18 Phase 1) happens in moderated
  sessions, not in the event stream.
- **`v3_avatar_consent` records only `accepted` and `source_type`** — the
  consent copy version is audited on the profile record (v3-p0-contract §4.2),
  not duplicated into analytics.

## 6. Metrics and kill criteria

V3-002 acceptance 4. Metric definitions live in the JSON artifact
(`metrics`), each naming the events and fields it reads; a test asserts every
reference resolves. Mapping to V3_SPEC §20:

| # | Kill criterion | Computable from | Notes |
|---|---|---|---|
| 1 | "Doesn't look like me" is the common first reaction | `identity_fidelity_rate` | Self-reported verdict, the honest available signal. |
| 2 | Garment silhouette is persistently altered | `garment_fidelity_complaint_rate`, `preview_hidden_rate` | A complaint proxy, not a measurement of distortion. |
| 3 | Entertainment value only, no effect on decisions | `decision_action_rate`, `post_preview_decision_action_rate`, `existing_item_alternative_rate`, `repeat_use_rate` | |
| 4 | Photo/consent asking depresses main-flow completion | `consent_prompt_completion_delta`, `consent_acceptance_rate` | Needs the `when_in_flow` rule in §3. |
| 5 | The visual entry makes Should I Buy slower or heavier | `time_to_decision_seconds` (segmented), `decision_completion_rate`, `preview_request_rate`, `fallback_decision_retention_rate` | |
| 6 | Users read the result as a size guarantee | **Qualitative only** | A comprehension failure; only moderated sessions and support reports can detect it. Do not proxy it with a number. |
| 7 | Cost not controllable via cache and on-demand triggering | `preview_cache_hit_rate`, `generations_per_useful_decision` | |
| 8 | Persistently worse than comparable products users have seen | **Qualitative only** | A relative judgment with no in-app referent; benchmark review. |
| 9 | Minimum quality requires 3D capture or elaborate shooting | `preview_hidden_rate` informs it | The decision itself is a benchmark/product call. |
| 10 | Visual work is sacrificing Today and import-to-first-outfit | **Qualitative only** | Program allocation, not a user-behavior signal. |

The three qualitative-only criteria are declared in the artifact
(`qualitative_only_kill_criteria`) and a test asserts criteria 1–10 are each
covered exactly once, by a metric or by that list. The rollout dashboard
(V3-023) must show them as review items, not as green numbers.

Release-threshold metrics from V3_SPEC §19 map onto the same set:
"no-avatar users still complete the decision" → `decision_completion_rate`
restricted to flows with no `v3_preview_requested`; "at most one extra explicit
action for an avatar-ready user" → `preview_request_rate` with
`avatar_status=ready`; "preview failure does not reduce completion" →
`fallback_decision_retention_rate`.

## 7. Deliberate enum narrowing

Two analytics enums are strict subsets of the API enums frozen in
v3-p0-contract. This is intentional, not drift:

| Analytics enum | API enum | Narrowing |
|---|---|---|
| `parse_status` on `v3_decision_completed` | `submitted\|parsed\|parse_failed` (§5.2) | `submitted` is a transient polling state; a decision event can only be emitted after parsing reached a terminal state. |
| `generation_status` on `v3_preview_outcome` | `queued\|processing\|quality_check\|ready\|failed\|rejected_by_quality_gate` (§6.2) | `v3_preview_outcome` fires once per generation, at the terminal state. Counting intermediate polls would inflate every preview denominator. |

Everything else is verbatim: decision states (V3-007), feedback verdicts
(V3-020), avatar states (§4.1), next actions (§5.3), decision-source types
(§5.2). Note that the preview `concern` enum here is the §6.1 one
(`volume|color|length|overall`) — the §5.3 decision `concern`
(`volume|color|redundancy|scene|none`) is a separate frozen enum and the two
must not be unified.

## 8. Changing this contract

1. Edit `app/common/analytics_events.py`.
2. Regenerate the artifact:

   ```bash
   uv run python -c "import json; from pathlib import Path; from app.common.analytics_events import contract_dict; Path('docs/v3-analytics-events.json').write_text(json.dumps(contract_dict(), indent=2) + '\n', encoding='utf-8', newline='\n')"
   ```

3. Copy `docs/v3-analytics-events.json` **and this file** to aidrobe-FE
   unchanged, and update `src/utils/v3AnalyticsEvents.ts` to match.
4. Run both drift tests. A field added on one side only fails them by design.
5. A breaking change (removing an event, removing an enum value, changing a
   field's meaning) bumps `schema_version`; additive changes do not.
