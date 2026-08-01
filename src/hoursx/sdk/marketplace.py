"""Marketplace index format.

The marketplace is deliberately dumb: a static JSON index listing plugins,
versions, sources, checksums, and requested permissions. The server can fetch
and present it; installing means recording the operator's permission grant —
code arrives via normal package installation, never executed by the index.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from hoursx.sdk.manifest import PluginPermission


class MarketplaceEntry(BaseModel):
    name: str
    version: str
    summary: str = ""
    author: str = ""
    source: str = Field(description="pip requirement or artifact URL")
    sha256: str = Field(default="", description="Artifact checksum, when source is an artifact")
    permissions: list[PluginPermission] = Field(default_factory=list)


class MarketplaceIndex(BaseModel):
    entries: list[MarketplaceEntry] = Field(default_factory=list)


async def fetch_marketplace_index(url: str) -> MarketplaceIndex:
    """Fetch and validate a remote marketplace index."""
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(url)
        response.raise_for_status()
        return MarketplaceIndex.model_validate(response.json())
