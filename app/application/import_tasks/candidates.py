"""Stable candidate identity shared by confirmation, processing and reads."""

import uuid
from typing import Any

from app.infrastructure.persistence.models.import_task import ImportTask

SINGLE_CANDIDATE_ID = "__single__"


def normalize_candidates(task: ImportTask) -> list[dict[str, Any]]:
    payload = task.candidate_payload
    if not payload:
        return []
    raw_items = payload.get("candidate_items")
    if not isinstance(raw_items, list):
        return [{**payload, "temp_item_id": payload.get("temp_item_id") or SINGLE_CANDIDATE_ID}]
    return [
        {
            **raw,
            "temp_item_id": raw.get("temp_item_id") or uuid.uuid5(task.id, str(index)).hex,
        }
        for index, raw in enumerate(raw_items)
        if isinstance(raw, dict)
    ]


def store_candidates(task: ImportTask, candidates: list[dict[str, Any]]) -> None:
    task.candidate_payload = {**candidates[0], "candidate_items": candidates}


def confirmed_item_ids(task: ImportTask) -> dict[str, str]:
    raw = (task.input_payload or {}).get("confirmed_items")
    if not isinstance(raw, dict):
        return {}
    confirmed = {str(key): str(value) for key, value in raw.items()}
    if SINGLE_CANDIDATE_ID in confirmed:
        candidates = normalize_candidates(task)
        if candidates:
            confirmed.setdefault(candidates[0]["temp_item_id"], confirmed[SINGLE_CANDIDATE_ID])
    return confirmed
