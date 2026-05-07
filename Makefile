.PHONY: install dev test lint format migrate-create migrate-apply openapi

install:
	uv sync

dev:
	fastapi dev app/main.py

test:
	pytest

lint:
	ruff check . && mypy app

format:
	ruff check --fix . && ruff format .

migrate-create:
	alembic revision --autogenerate -m "$(m)"

migrate-apply:
	alembic upgrade head

openapi:
	uv run python -c "from app.main import app; import json, yaml; s=app.openapi(); open('docs/openapi.json','w',encoding='utf-8').write(json.dumps(s, ensure_ascii=False, indent=2)); open('docs/openapi.yaml','w',encoding='utf-8').write(yaml.safe_dump(s, allow_unicode=True, sort_keys=False))"
	@echo "Wrote docs/openapi.json and docs/openapi.yaml"
