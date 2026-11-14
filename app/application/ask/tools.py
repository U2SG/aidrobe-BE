"""Dispatch read-only tools for a single Ask turn.

A tool reads the user's own data on the server; it never acts. Unknown tool
names and handler errors are reported back to the model as an error result,
so one bad call cannot fail the whole answer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from app.application.ports.llm import ToolResult, ToolSpec

logger = structlog.get_logger(__name__)

ToolHandler = Callable[[dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True)
class AskTool:
    spec: ToolSpec
    handler: ToolHandler


class ToolRegistry:
    def __init__(self, tools: Sequence[AskTool] = ()) -> None:
        self._tools = {tool.spec.name: tool for tool in tools}

    @property
    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def __bool__(self) -> bool:
        return bool(self._tools)

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            logger.warning("ask.tool.unknown", tool=name)
            return ToolResult(content={"error": f"unknown tool {name}"}, summary="")
        try:
            return await tool.handler(arguments)
        except Exception as exc:  # a tool failure degrades, it never sinks the answer
            logger.warning("ask.tool.failed", tool=name, error_type=type(exc).__name__)
            return ToolResult(content={"error": "tool failed"}, summary="")
