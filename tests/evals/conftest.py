"""Eval-suite conftest.

The recommendation evals are pure, fixture-based, and never touch the database.
`tests/conftest.py` declares two autouse fixtures (`create_schema` session-scope
and `_truncate_between_tests` function-scope) that both spin up a Postgres
testcontainer. They cascade into every subdirectory, including this one.

We override both by name with no-op fixtures that do NOT request the db-url
fixtures, so `uv run pytest tests/evals` runs in ~1s with no container pull.
"""

import pytest


@pytest.fixture(scope="session", autouse=True)
def create_schema() -> None:  # type: ignore[override]
    """No-op override of tests/conftest.py — evals need no schema."""
    return None


@pytest.fixture(autouse=True)
def _truncate_between_tests() -> None:  # type: ignore[override]
    """No-op override of tests/conftest.py — evals never write to the DB."""
    return None
