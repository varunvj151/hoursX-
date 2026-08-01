"""Tool registry with grant-based visibility.

The registry holds every known tool; an agent sees only tools matching its
profile's grant patterns (fnmatch globs like ``fs.*`` or exact names). Tools an
agent is not granted are invisible to the model — never advertised, never
executable — so denial happens by omission, not by runtime failure.
"""

from __future__ import annotations

from fnmatch import fnmatch

from hoursx.providers.types import ToolDescriptor
from hoursx.tools.base import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"tool {name!r} already registered")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def granted(self, grants: list[str]) -> list[Tool]:
        """Tools visible under *grants* (deterministic order for prompt caching)."""
        return [
            tool
            for name, tool in sorted(self._tools.items())
            if any(fnmatch(name, pattern) for pattern in grants)
        ]

    def descriptors(self, grants: list[str]) -> list[ToolDescriptor]:
        return [tool.spec.descriptor() for tool in self.granted(grants)]

    def is_granted(self, name: str, grants: list[str]) -> bool:
        return name in self._tools and any(fnmatch(name, pattern) for pattern in grants)
