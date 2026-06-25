"""Pytest driver for the recommendation-quality eval harness (BE #50).

Loads every fixture under ``fixtures/`` and runs the deterministic + heuristic
checkers. A fixture is parametrized by file name so a failure points straight at
the offending scenario, and the assertion message names the regressed
constraint.

Two fixture modes:
  * golden     — `expect_failures` absent/empty → ALL checks must pass.
  * regression — `expect_failures: ["constraint", ...]` → those checks MUST fail
                 (proving the harness has teeth) and no OTHER check may fail.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.evals.checkers import CheckResult, evaluate, llm_judge

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load_fixtures() -> list[tuple[str, dict[str, Any]]]:
    cases: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        cases.append((path.stem, data))
    return cases


_FIXTURES = _load_fixtures()


def test_fixtures_present() -> None:
    """Acceptance criterion: at least 5 core scenarios."""
    golden = [name for name, fx in _FIXTURES if not fx.get("expect_failures")]
    assert len(golden) >= 5, f"need >=5 golden scenarios, found {len(golden)}: {[n for n, _ in _FIXTURES]}"


@pytest.mark.parametrize("name,fixture", _FIXTURES, ids=[n for n, _ in _FIXTURES])
def test_recommendation_fixture(name: str, fixture: dict[str, Any]) -> None:
    results: list[CheckResult] = evaluate(fixture)
    failed = {r.constraint for r in results if not r.ok}
    expected = set(fixture.get("expect_failures", []))

    # Build an actionable report regardless of outcome.
    report = "\n".join(f"  [{'PASS' if r.ok else 'FAIL'}] {r.constraint}: {r.message}" for r in results)

    if expected:
        missing = expected - failed
        unexpected = failed - expected
        assert not missing, (
            f"regression fixture {name!r}: expected these checks to FIRE but they passed: "
            f"{sorted(missing)}\n{report}"
        )
        assert not unexpected, (
            f"regression fixture {name!r}: unexpected checks regressed: {sorted(unexpected)}\n{report}"
        )
    else:
        assert not failed, f"golden fixture {name!r} regressed on: {sorted(failed)}\n{report}"


def test_llm_judge_off_by_default() -> None:
    """The optional LLM-judge layer is inert unless explicitly enabled, and never
    runs in CI."""
    assert llm_judge({}, enabled=False) == []
    with pytest.raises(NotImplementedError):
        llm_judge({}, enabled=True)
