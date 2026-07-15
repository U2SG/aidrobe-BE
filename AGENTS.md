# Project Instructions

# graphify
- **graphify** (`~/.Codex/skills/graphify/SKILL.md`) - any input to knowledge graph. Trigger: `/graphify`
When the user types `/graphify`, invoke the Skill tool with `skill: "graphify"` before doing anything else.

## Aidrobe Backend Notes

- Treat `docs/aidrobe-product-experience-optimization-plan-v1.0.md` as the current V2 execution source of truth.
- Use `docs/12_V1范围精简方案.md` only as the V1 flag-off regression baseline when it conflicts with V2.
- Keep backend changes aligned with the current FastAPI structure under `app/`: presentation, application, infrastructure, jobs, and shared config/common modules.
- Use `.env.example` for documented local configuration. Do not commit `.env`.
- Tests and checks run only in GitHub CI; do not run pytest, ruff, mypy, builds, or migration round-trips locally unless the user explicitly overrides this preference.
- `X-User-Id` is a development-only authentication shortcut. Production mode must reject it until real auth is implemented.
- Before handoff after backend changes, ensure GitHub CI covers `uv run pytest -q`, `uv run ruff check .`, `uv run mypy app`, and an Alembic upgrade/downgrade/upgrade cycle when migrations change; report CI status without substituting local runs.
