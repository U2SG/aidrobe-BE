# Recommendation quality eval harness (BE #50)

A lightweight, deterministic, fixture-based eval suite for recommendation
output quality. Runs under pytest (so it runs in CI), touches **no database**,
and **never calls a real LLM**.

## Run

```bash
uv run pytest tests/evals -q
```

Fast (~1s): `tests/evals/conftest.py` overrides the DB-spinning autouse
fixtures from `tests/conftest.py`, so no Postgres testcontainer is started.

## Layout

- `checkers.py` — the eval engine. Three layers:
  1. **Contract / deterministic** (always run): schema validity, referenced item
     ids exist / no hallucinated items, required reasoning fields present,
     recent-outfit diversity when wear history exists.
  2. **Heuristic** (always run): generic/mock copy detection, repeated-phrase
     detection, minimum specificity, plus per-fixture keyword-alignment and
     focused-item / negative-preference assertions.
  3. **LLM judge** — OFF by default, an inert stub. Never calls a real LLM in v1.
- `fixtures/*.json` — one scenario per file.
- `test_recommendation_evals.py` — loads every fixture and asserts.

## Fixture shape

Extends the issue's example shape with a required `output` field (the
recommendation under test, `TodayRecommendationResponse`-shaped) so the suite is
deterministic without an LLM call:

```json
{
  "name": "...",
  "wardrobe":         [ {"id": "<uuid>", "category": "...", "color": "...", "fit": "..."} ],
  "user_preferences": {"negative": [...], "positive": [...]},
  "request":          "free-text user ask",
  "request_context":  {"weather": {...}, "occasion": "...", "focused_item_id": "<uuid>"},
  "recent_outfits":   [ {"item_ids": ["<uuid>", ...]} ],
  "output":           { ...TodayRecommendationResponse JSON... },
  "assertions":       [ {"type": "...", ...} ],
  "expect_failures":  ["constraint_name", ...]
}
```

- **Golden** fixtures omit `expect_failures` → every check must pass.
- **Regression** fixtures set `expect_failures` → exactly those checks must fire
  (and no others). These prove the harness has teeth.

## Assertion types

| `type`                     | fields                       | meaning                                              |
|----------------------------|------------------------------|------------------------------------------------------|
| `must_include_item`        | `item_id`                    | item appears in some recommended outfit (focused)    |
| `must_not_include_item`    | `item_id`                    | item is avoided                                       |
| `must_reference_preference`| `value`                      | copy text references the substring                    |
| `copy_contains`            | `keywords` (any-of), `label` | weather/occasion keyword alignment                    |
| `no_attribute_value`       | `attribute`, `values`        | no recommended item has a disliked attribute value    |

## Adding a scenario

Drop a new JSON file in `fixtures/`. It is auto-discovered and parametrized by
file name, so a failure points straight at the scenario and names the regressed
constraint. To add a new constraint, add a checker in `checkers.py` (contract /
heuristic) or a new assertion handler in the `_ASSERTIONS` dispatch table.
