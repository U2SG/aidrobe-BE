.PHONY: install dev test lint format migrate-create migrate-apply

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
