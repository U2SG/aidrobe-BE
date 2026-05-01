# Project Instructions

# graphify
- **graphify** (`~/.Codex/skills/graphify/SKILL.md`) - any input to knowledge graph. Trigger: `/graphify`
When the user types `/graphify`, invoke the Skill tool with `skill: "graphify"` before doing anything else.

## Aidrobe Backend Notes

- Treat `docs/12_V1范围精简方案.md` as the V1 Slim product scope source of truth.
- Keep backend changes aligned with the current FastAPI structure under `app/`: presentation, application, infrastructure, jobs, and shared config/common modules.
- Use `.env.example` for documented local configuration. Do not commit `.env`.
- Tests use PostgreSQL through testcontainers, so Docker Desktop must be running before the full test suite.
- `X-User-Id` is a development-only authentication shortcut. Production mode must reject it until real auth is implemented.
- Before handoff after backend changes, run `uv run pytest -q`, `uv run ruff check .`, and `uv run mypy app`. Run an Alembic upgrade/downgrade/upgrade cycle when migrations change.
