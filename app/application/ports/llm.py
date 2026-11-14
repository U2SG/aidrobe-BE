from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class LlmAnswer:
    """Result of a free-form LLM ask. `tokens_used` is the provider-reported
    (or estimated) total token count, recorded for cost-guard accounting."""

    text: str
    tokens_used: int


class LlmPort(Protocol):
    async def polish_reason(self, prompt: str) -> str: ...

    async def ask(
        self, system_prompt: str, question: str, *, json_mode: bool = False
    ) -> LlmAnswer:
        """Answer a free-form styling question. `system_prompt` carries the
        already-sanitized wardrobe context; `question` is the user's prompt.
        When `json_mode` is set, the provider is constrained to emit a JSON
        object (used for structured `{answer, referenced_items}` output).
        Implementations with no key configured return an empty `text` so the
        caller can degrade to a 503."""
        ...


@dataclass(frozen=True)
class ToolSpec:
    """A function the model may call during an Ask turn (OpenAI tool format)."""

    name: str
    description: str
    # JSON Schema for the arguments object.
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """What a tool returns: `content` goes back to the model (JSON-serialisable),
    `summary` is the one-line, user-facing note ("看了明天的天气：小雨 14°")."""

    content: Any
    summary: str


@dataclass(frozen=True)
class ToolUse:
    tool: str
    summary: str


@dataclass(frozen=True)
class ToolAskAnswer(LlmAnswer):
    used: list[ToolUse] = field(default_factory=list)
    rounds: int = 1


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[ToolResult]]


@runtime_checkable
class ToolCallingLlm(Protocol):
    """Providers that can run a multi-round tool-calling turn."""

    async def ask_with_tools(
        self,
        system_prompt: str,
        question: str,
        tools: Sequence[ToolSpec],
        execute: ToolExecutor,
        *,
        json_mode: bool = False,
        max_rounds: int = 4,
    ) -> ToolAskAnswer:
        """Let the model call `tools` (executed by `execute`) for up to
        `max_rounds` model calls, then return its final answer. The last round
        offers no tools, so the model must answer with what it has. Tokens are
        summed over every round; `used` lists each executed tool in order.
        A provider error returns empty `text`, like `ask`."""
        ...
