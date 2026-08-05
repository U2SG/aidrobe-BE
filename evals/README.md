# Deterministic recommendation evaluations

This corpus is the first Recommendation Quality Engineering baseline from GitHub issue #129. It runs the real candidate construction, filtering, scoring, relationship adjustment, suppression, and selection logic in `RecommendationEngine`; it does not call an endpoint, database, weather provider, or LLM.

## Run it

```bash
make recommendation-eval
```

The command writes two deterministic reports:

- `evals/reports/recommendation-eval.json` for machine comparison
- `evals/reports/recommendation-eval.md` for human review and CI artifacts

Exit code `0` means every declared hard invariant passed. Exit code `1` means the reports were valid but at least one hard invariant regressed. Exit code `2` means the scenario corpus, committed schema, or generated report was malformed.

Score and candidate-rank changes are always reported, but they do not fail CI in v1.

## Corpus and schema

`evals/recommendations/schema.v1.json` is the versioned JSON Schema. `scenarios.v1.json` contains only synthetic data and is also validated by strict Pydantic models (`extra="forbid"`) before any scenario runs.

Each scenario contains:

- `id` and `description`
- `user_settings`, including IANA timezone, location, commute exposure, and preferences
- `wardrobe` item features plus `available`, `laundry`, or `unavailable` state
- `wear_history`, including recent main items, recent combinations, and temperature-mismatch events
- `learned_signals`, including style/fit profiles, disliked colors, relationship edges, suppression signatures, and diversity behavior
- recorded `weather` plus target `request` date, scene, active constraints, and optional focused item
- `expected` hard invariants, acceptable slot sets, allowed/forbidden outcomes, and reason sources

The `selected_outfit` expectations apply to the primary (first) engine outfit. Availability/laundry, weather, and reason-grounding metrics inspect every returned outfit.

The current Today engine has no focused-item input port. In v1, `focused_item_id` is therefore an explicit output assertion over the real ranking result, not a hidden pinning adapter. Focus scenarios must include a competing candidate so the assertion can detect a ranking change. Adding a production focus/pinning contract is separate product work.

## Determinism boundaries

- The corpus fixes the clock at `2026-08-05T00:00:00Z` and the random seed at `129`.
- The fixed clock drives local-date weather selection and indoor-climate event windows.
- `FakeWeatherAdapter` returns only recorded fixture weather. Future dates never fall back to current weather.
- `InMemoryScenarioRepository` supplies wardrobe/history/learned inputs and filters laundry/unavailable items before the production engine, mirroring the wearable-item persistence boundary.
- The engine is constructed with `llm=None`, deliberately bypassing prose polishing while retaining deterministic structured reasoning.
- Reports use the fixed clock, sorted JSON keys, ordered fixtures, and LF newlines. No wall-clock time or machine path is serialized.

## Metric vector

Every scenario reports separate values for:

- `hard_constraint_satisfaction` and each named hard-invariant check
- `outfit_slot_completeness`
- `unavailable_or_laundry` violations
- `weather_suitability` violations
- `focused_item_inclusion` (nullable when no focused item is required)
- `recent_combination` exact-repeat, maximum Jaccard similarity, and novelty
- `reason_grounding`, including referenced item IDs, snapshot/outfit membership, sources, and malformed entries
- `outcome_constraints` for required/allowed/forbidden primary-outfit outcomes
- `selected_candidate` score, candidate count, and rank within its outfit type

`forecast_unavailable_honest` and `indoor_climate_applied` are also explicit scenario metrics.

## Add a scenario

1. Add one object to `evals/recommendations/scenarios.v1.json`; keep every item and event synthetic.
2. Give every wardrobe item a stable scenario-local ID and use only those IDs in history, relationships, suppression, focus, and expected outcomes.
3. Record current weather and/or a target-date forecast. Use a JSON `null` forecast to test honest future-weather degradation.
4. List only the invariants that should block CI under `expected.hard_invariants`.
5. Declare primary-outfit requirements and allowed/forbidden item/category outcomes under `expected.selected_outfit`.
6. List the structured reason sources the real engine must produce.
7. Run `make recommendation-eval`, inspect both reports, and run it again if the change depends on deterministic ordering.

`schema.v1.json` is generated from `RecommendationCorpus.model_json_schema()` and compared byte-independently as a JSON object on every run. When introducing a field or invariant, update `models.py`, regenerate the schema, update this README, and run the eval; CI fails if the committed schema drifts.

## Interpret a failure

- `status_ready`: the engine could not construct a recommendation.
- `slot_completeness`: the primary outfit matched none of the scenario's acceptable slot sets.
- `available_items_only`: a laundry/unavailable item crossed the in-memory persistence boundary.
- `weather_suitable`: a cold outfit lacked outerwear or a rainy outfit used light shoes.
- `focused_item_included`: the required focused item was absent from the primary outfit.
- `no_exact_recent_repeat`: the primary item set exactly repeated a recent combination.
- `reason_grounded`: a structured reason was malformed, cited an item outside its outfit/snapshot, or omitted an expected source.
- `outcome_constraints`: the primary outfit missed required items or violated an allowed/forbidden item/category declaration.
- `forecast_unavailable_honest`: a future forecast was unavailable but current weather leaked into the recommendation or its reasons.
- `indoor_climate_applied`: repeated mismatch evidence did not produce a removable layer plus grounded indoor-climate reason.

Open the detailed scenario section in the Markdown report or the corresponding metric object in JSON. Do not treat a score/rank delta alone as a failure until the corpus is calibrated and an explicit threshold is agreed.
