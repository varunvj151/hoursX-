"""Plugin manifest schema — the contract between a plugin and the platform."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from hoursx.tools.base import Tool


class PluginPermission(StrEnum):
    """Capabilities a plugin can request; operators grant a subset at install."""

    NETWORK = "network"  # outbound HTTP
    FILESYSTEM = "filesystem"  # session-sandbox file access
    SHELL = "shell"  # subprocess execution
    KNOWLEDGE = "knowledge"  # read the workspace knowledge base
    MEMORY = "memory"  # read/write long-term memory


class PluginTool(BaseModel):
    """One tool contributed by a plugin, tagged with the permissions it needs.
    A tool whose permissions are not all granted is not registered."""

    model_config = {"arbitrary_types_allowed": True}

    tool: Tool
    needs: list[PluginPermission] = Field(default_factory=list)


class PluginManifest(BaseModel):
    """Identity + contributions of one plugin."""

    model_config = {"arbitrary_types_allowed": True}

    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,63}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    summary: str = Field(max_length=300)
    author: str = ""
    homepage: str = ""
    tools: list[PluginTool] = Field(default_factory=list)

    @field_validator("tools")
    @classmethod
    def _tool_names_are_namespaced(cls, tools: list[PluginTool]) -> list[PluginTool]:
        for entry in tools:
            if "." not in entry.tool.spec.name:
                raise ValueError(
                    f"plugin tool {entry.tool.spec.name!r} must be namespaced (plugin.tool)"
                )
        return tools

    def granted_tools(self, granted: set[PluginPermission]) -> list[Tool]:
        """Tools whose permission needs are fully covered by *granted*."""
        return [entry.tool for entry in self.tools if set(entry.needs) <= granted]
