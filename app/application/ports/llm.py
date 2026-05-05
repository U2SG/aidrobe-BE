from typing import Protocol


class LlmPort(Protocol):
    async def polish_reason(self, prompt: str) -> str: ...
