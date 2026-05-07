Define the allowed wardrobe item status lifecycle for user-facing APIs and future automation.

## Transition Matrix

Legend: P = user-triggered, S = system-triggered by future laundry module, X = forbidden, — = no-op same state.

Note: `docs/backend-gaps-plan.md` section 2.4 defines the five states and the `wear`/freshness decision, but does not include explicit matrix values. This matrix uses the Phase A-3 fallback assumption: active ownership states can be maintained by the user, laundry automation owns the future clean transition from `dirty` to `wearable`, and `donated` is terminal.

| From \ To | wearable | unavailable | dirty | lost | donated |
| --- | --- | --- | --- | --- | --- |
| wearable | — | P | P | P | P |
| unavailable | P | — | P | P | P |
| dirty | S | P | — | P | P |
| lost | P | P | X | — | X |
| donated | X | X | X | X | — |

## Decision Note

The `wear` action does NOT auto-set status to `dirty`. Freshness is a derived value computed from `wear_history` plus per-category rules in `app/config/freshness_rules.yaml`, to be added later. `dirty` status is set only by explicit user action or by the future laundry module.

## API Enforcement (current)

At the `PATCH /v1/wardrobe-items/{id}` boundary, only user-triggered `P` transitions are legal:

- From `wearable`: `unavailable`, `dirty`, `lost`, `donated`
- From `unavailable`: `wearable`, `dirty`, `lost`, `donated`
- From `dirty`: `unavailable`, `lost`, `donated`
- From `lost`: `wearable`, `unavailable`
- From `donated`: no outbound user transitions

Same-state PATCH requests are no-ops and should remain accepted.

System-triggered `S` transitions are not accepted through this user PATCH boundary.

## Future

The V4+ laundry module may add system-triggered transitions, beginning with `dirty` to `wearable` after a wash cycle completes, and may later set `dirty` from laundry-specific workflows without changing the `wear` action semantics.
