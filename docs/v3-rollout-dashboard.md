# V3 Rollout Dashboard and Readiness Contract

Feature: **V3-023**. Authority: `V3_SPEC.md` §18/§19/§20 and the immutable
acceptance criteria in `feature_list.json` outrank this document.
`docs/v3-analytics-events.md` (V3-002) freezes the events and metrics this
dashboard is built from; V3-023 adds no event, no field and no metric.

This document and `docs/v3-rollout-dashboard.json` are copied **byte-identically**
into aidrobe-FE.

| Artifact | Role |
|---|---|
| `app/common/rollout_dashboard.py` (BE) | Generator of record. |
| `docs/v3-rollout-dashboard.json` (both repos) | The contract both sides are tested against. |
| `tests/unit/test_v3_rollout_dashboard.py` (BE) | Computability, coverage and readiness invariants. |
| `scripts/v3RolloutDashboard.test.ts` (FE) | Mirror equality + the 7 fe-owned emitters. |

The artifact has twelve top-level keys; the four to read first are
`kill_criteria` (§3), `pipeline_prerequisites` (§5), `readiness_gates` (§6) and
`seed_study` (§7).

**This contract enables nothing.** All three `ENABLE_V3_*` flags remain `False`
in committed configuration; §5 lists the decisions a person still owes.

## 1. What this dashboard measures, and what it refuses to

The rollout gate is usefulness, trust, fallback and cost — never generation
volume (`V3_SPEC` §18). Two consequences are built into the contract rather
than left to reviewer discipline:

- There is no "previews generated" panel. The cost panel divides paid
  generations *by useful decisions*, so more generations for the same decisions
  looks **worse**, not better.
- `generations_per_useful_decision` counts only `cache_hit=false` outcomes plus
  the retries **on those same rows**, because a cache hit costs nothing. That
  scope is stated explicitly in the metric definition: a cache hit carries the
  *original* asset's `retry_count` (correctly — the field describes the asset's
  history), so summing retries across all outcomes would re-charge one paid
  regeneration on every free cache read and inflate the very ratio the cost gate
  reads.

## 2. Panels

Eight panels. The first six are required by V3-023's acceptance criteria; the
last two exist because V3-002 froze kill-criteria metrics for visual trust
(§20.1/§20.2) and consent cost (§20.4) that none of the six required dimensions
names. Dropping them would leave a frozen metric unsurfaced and two kill
criteria unwatched.

| Panel | Headline metric | Also on the panel |
|---|---|---|
| `decision_completion` | `decision_completion_rate` | `decision_action_rate`, `existing_item_alternative_rate` |
| `preview_usefulness` | `post_preview_decision_action_rate` | `preview_request_rate`, `fallback_decision_retention_rate` |
| `repeat_use` | `repeat_use_rate` | — |
| `hidden_quality_rate` | `preview_hidden_rate` | — |
| `latency` | `time_to_decision_seconds` | — |
| `cost_per_useful_decision` | `generations_per_useful_decision` | `preview_cache_hit_rate` |
| `visual_trust` | `identity_fidelity_rate` | `garment_fidelity_complaint_rate` |
| `consent_cost` | `consent_prompt_completion_delta` | `consent_acceptance_rate` |

Each of V3-002's 15 metrics appears on **exactly one** panel. Both halves are
asserted: an unsurfaced metric is a blind spot, and a metric on two panels reads
as two independent signals when it is one.

## 3. Computability

**What "computable from collected events" means here, stated before anything
else, because it is the one claim in this document that can be misread.** It
means the definition of every kill criterion terminates in fields of events this
system emits — no criterion depends on a field nobody sends, a metric nobody
defined, or a number somebody would have to invent. It does **not** mean numbers
exist today: no telemetry sink is chosen, so nothing is collected yet, and
choosing one is a human gate (`telemetry_sink`). That gap is declared as data in
`pipeline_prerequisites`, asserted by a test, and named in §5 — the contract is
fully specified and not yet fed. Computability is a property of the definitions;
having data is a property of the pipeline. This feature delivers the first and
is explicitly blocked on a person for the second.

The chain each kill criterion depends on:

```text
kill criterion -> metric -> event field -> event -> emitter on the owning side -> sink
```

Contract links are asserted by the two drift tests. FE #157 additionally tests
the transport against a synthetic collector; production durability remains open. A field resolves
if it is an envelope field or is carried by **at least one** of the metric's
events — the union, not the intersection, because a metric legitimately joins
events with different field sets (`consent_prompt_completion_delta` reads
`context` off `v3_avatar_prompt_shown` while joining decision events that carry
no such field).

Kill-criteria coverage is a strict partition of §20's ten criteria:

| Coverage | Criteria |
|---|---|
| Metric-backed | 1, 2, 3, 4, 5, 7, 9 |
| Qualitative-only | 6, 8, 10 |

The qualitative three are V3-002's own declaration and V3-023 may not
re-classify them in either direction (asserted). Faking a proxy number for
"users think it's a size guarantee" would be worse than admitting the gap.

## 4. Latency and cost have no event fields of their own

- **Latency** is an `occurred_at` delta joined on `flow_id`. No event carries a
  duration: a client-reported duration is unverifiable and an open value space.
- **Cost** needs a price, and choosing a provider is a human gate. So
  `cost_model.unit_price_per_generation` is `null` and stays `null`. A
  placeholder number would read like an approved budget (asserted).

## 5. Why every panel is empty today

`pipeline_prerequisites` in the artifact records this as data. Met: the frozen
contract, and an emitter for all 11 events on the declared owning side. Unmet:

1. **No approved deployed durable collector.** FE #157 now registers the default-off
   release dispatcher; FE relay ingestion and the four BE-owned events share the
   same API/worker delivery port. A synthetic local SQLite-backed HTTP fixture
   can verify joined flows, but it does not choose a production processor,
   guarantee retention/deletion/access, or prove a deployed build. Those remain
   the `telemetry_sink` human gate. The pipeline prerequisite stays **unmet**.
2. **Flags off**, so no V3 event is produced at all. Deliberately unmet —
   V3-023 acceptance 3 requires it.

## 6. Readiness: verified capability vs pending approvals

`readiness_gates` carries `V3_SPEC` §19's own four groups verbatim, so the report
is auditable against the spec rather than against a restructuring of it. Each
threshold has one of three statuses:

| Status | Means |
|---|---|
| `verified` | This program produced evidence for the threshold itself. Requires evidence, forbids a blocker. |
| `mechanism_only` | The code path is proven; the field outcome is not. |
| `unverified` | No evidence yet. Requires a named blocker. |

`mechanism_only` is the category that keeps the report honest. A test proves a
failed preview never *erases* a decision; it cannot prove that failures do not
depress the completion *rate* in the field. Those are different claims and only
the first was earned, so the gate says so instead of rounding up to `verified`.

Nothing in the `visual` or `product_value` groups is `verified`, and a test
enforces that: both need a real provider and real users, so promoting one must be
a deliberate act that edits a test, never a drive-by. Every non-verified gate
names its blocker and, where the blocker is a person's decision, the
`open_human_gates` key that owns it.

## 7. The seed study that would discharge the unverified gates

`V3_SPEC` §18 Phase 1 is qualitative on purpose — "目标不是证明增长，而是验证可
用性" — so `seed_study` in the artifact carries all five of its questions, what a
moderator should actually observe for each, the §19 gates answering it would move
off `unverified`, and the one frozen metric (if any) that corroborates the
finding once a sink exists.

| §18 Phase 1 question | Discharges | Corroborating metric |
|---|---|---|
| Difference from platform try-on understood? | platform-comparison gate, size-guarantee gate | — |
| Willing to authorize a photo? | — | `consent_acceptance_rate` |
| Which moment needs the preview? | — | `preview_request_rate` |
| Does it change or confirm a decision? | confidence gate, not-merely-entertaining gate | `post_preview_decision_action_rate` |
| Which distortions destroy trust? | looks-like-me gate, garment-believability gate | `garment_fidelity_complaint_rate` |

The load-bearing assertion is coverage in the other direction: **every** gate
whose blocker is the `seed_cohort` human gate must be discharged by at least one
question. A seed round designed to leave one of them unanswered fails
`test_every_seed_blocked_gate_is_covered_by_a_seed_question`. Two questions
discharge no gate and are kept anyway because §18 lists them and each has a
corroborating metric; a question with neither is rejected by test.

Recruiting the cohort and processing real photos is itself the `seed_cohort`
human gate. This section is the plan, not its approval.

## 8. Changing this contract

1. Edit `app/common/rollout_dashboard.py`.
2. Regenerate the artifact:

   ```bash
   uv run python -c "import json; from pathlib import Path; from app.common.rollout_dashboard import contract_dict; Path('docs/v3-rollout-dashboard.json').write_text(json.dumps(contract_dict(), indent=2) + '\n', encoding='utf-8', newline='\n')"
   ```

3. Copy `docs/v3-rollout-dashboard.json` **and this file** to aidrobe-FE
   unchanged.
4. Run both drift tests, plus `./init.sh contract` from the workspace — a
   one-sided edit passes each repo's own CI and is caught only there. V3-023
   extended that check to compare **both** files in this pair across the repos
   (CR-stripped, because a Windows working copy checks out CRLF while git stores
   LF); before that extension the sentence you are reading was aspirational.
5. Marking a readiness gate `verified` requires evidence and forbids a blocker
   (asserted). Promoting a `visual` or `product_value` gate additionally fails
   `test_no_gate_is_verified_on_visual_or_product_value` by design: those two
   groups need a real provider and real users, so promoting one must be a
   deliberate act with a test edit, never a drive-by.
