"""Composable stylist prompt modules (BE#66).

Small, pure, locale-aware functions + data that assemble the LLM prompts shared
across recommendation features (Ask today; Today reason-polish; Should-I-Buy
once it grows an LLM call). Kept as functions + data rather than a fluent
builder class: there is currently one complex consumer (Ask), so a builder would
be premature abstraction. Revisit if a third real consumer appears.

Layers:
- `persona`            — one consistent stylist voice across features.
- `preference_memory`  — durable preferences rendered as PRIORITIZED rules.
- `reasoning_policy`   — the dimensions the model must reason over.
- `response_contracts` — the single source of each LLM response JSON contract.
- `self_critique`      — deterministic (no second LLM call) validators.
- `assemble`           — trivial ordered join of the sections above.
"""
