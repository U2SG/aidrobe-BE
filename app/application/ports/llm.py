from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LlmAnswer:
    """Result of a free-form LLM ask. `tokens_used` is the provider-reported
    (or estimated) total token count, recorded for cost-guard accounting."""

    text: str
    tokens_used: int


class LlmPort(Protocol):
    async def polish_reason(self, prompt: str) -> str: ...

    async def ask(self, system_prompt: str, question: str) -> LlmAnswer:
        """Answer a free-form styling question. `system_prompt` carries the
        already-sanitized wardrobe context; `question` is the user's prompt.
        Implementations with no key configured return an empty `text` so the
        caller can degrade to a 503."""
        ...
