"""Makes `tests` a package so the repository root stays the import root.

Without this file, pytest prepends `tests/` to `sys.path` for every test living
in a package-ified subdirectory (`tests/services`, `tests/unit`, `tests/evals`).
`tests/evals` then shadows the top-level `evals` package, so
`from evals.recommendations.replay import ...` resolves to the test package and
fails with `ModuleNotFoundError: No module named 'evals.recommendations'`.
"""
