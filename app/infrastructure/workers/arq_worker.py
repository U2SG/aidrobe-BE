"""Backward-compatible arq worker module.

Prefer:
    arq app.infrastructure.workers.worker_main.WorkerSettings
"""

from app.infrastructure.workers.worker_main import WorkerSettings

__all__ = ["WorkerSettings"]
