"""DashScope wanx2.1-imageedit — background-only mask repaint for OOTD posters.

`description_edit_with_mask` repaints the white-mask region per the prompt and
keeps the black-mask region (person + clothes) pixel-for-pixel — that's the
hallucination-control guarantee. Base/mask are fed as base64 data URIs so
private-bucket selfies never need a public URL (same approach as QwenVision).

The API is async: POST creates a task, GET polls it. Verified end-to-end in a
spike (~14s, $0.02/image).
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import httpx

from app.config.settings import Settings


def _data_uri(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


class WanxImageGeneration:
    def __init__(self, settings: Settings) -> None:
        if not settings.QWEN_API_KEY:
            raise ValueError("QWEN_API_KEY is required for wanx image edit")
        self.settings = settings

    async def edit_with_mask(
        self, *, base_image: bytes, mask_image: bytes, prompt: str
    ) -> bytes:
        s = self.settings
        payload: dict[str, Any] = {
            "model": s.QWEN_IMAGEEDIT_MODEL,
            "input": {
                "function": "description_edit_with_mask",
                "prompt": prompt,
                "base_image_url": _data_uri(base_image),
                "mask_image_url": _data_uri(mask_image),
            },
            "parameters": {"n": 1},
        }
        async with httpx.AsyncClient(timeout=s.IMAGEEDIT_TIMEOUT_SECONDS) as client:
            create = await client.post(
                s.QWEN_IMAGEEDIT_BASE_URL,
                headers={
                    "Authorization": f"Bearer {s.QWEN_API_KEY}",
                    "Content-Type": "application/json",
                    # Image models are slow; the HTTP API only supports the
                    # async create-then-poll flow.
                    "X-DashScope-Async": "enable",
                },
                json=payload,
            )
            create.raise_for_status()
            task_id = str(create.json()["output"]["task_id"])
            result_url = await self._poll(client, task_id)
            img = await client.get(result_url)
            img.raise_for_status()
            return img.content

    async def _poll(self, client: httpx.AsyncClient, task_id: str) -> str:
        url = f"{self.settings.QWEN_TASK_POLL_BASE_URL}/{task_id}"
        headers = {"Authorization": f"Bearer {self.settings.QWEN_API_KEY}"}
        # wanx finishes in ~5-15s; 40 * 3s = 120s ceiling before we give up.
        for _ in range(40):
            await asyncio.sleep(3)
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            output: dict[str, Any] = resp.json()["output"]
            status = output.get("task_status")
            if status == "SUCCEEDED":
                results = output.get("results") or []
                if not results or not results[0].get("url"):
                    raise RuntimeError("wanx task succeeded but returned no image url")
                return str(results[0]["url"])
            if status == "FAILED":
                raise RuntimeError(f"wanx task failed: {output.get('message') or output}")
        raise TimeoutError(f"wanx task {task_id} did not complete within poll budget")
