"""Plugin visibility and marketplace listing."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from hoursx.api.deps import Actor, get_services, require
from hoursx.api.schemas import PluginOut
from hoursx.auth import Permission
from hoursx.sdk.discovery import discover_plugins
from hoursx.sdk.marketplace import MarketplaceEntry, fetch_marketplace_index
from hoursx.services import AppServices

router = APIRouter(prefix="/v1/plugins", tags=["plugins"])


@router.get("", response_model=list[PluginOut])
async def list_loaded_plugins(
    actor: Actor = Depends(require(Permission.OBSERVE)),
    services: AppServices = Depends(get_services),
) -> list[PluginOut]:
    """Plugins currently loaded in this deployment (entry points + local dir)."""
    report = discover_plugins(Path(services.settings.plugin_dir))
    return [
        PluginOut(
            name=manifest.name,
            version=manifest.version,
            summary=manifest.summary,
            tools=[entry.tool.spec.name for entry in manifest.tools],
        )
        for manifest in report.loaded
    ]


@router.get("/marketplace", response_model=list[MarketplaceEntry])
async def list_marketplace(
    actor: Actor = Depends(require(Permission.PLUGINS_MANAGE)),
    services: AppServices = Depends(get_services),
) -> list[MarketplaceEntry]:
    url = services.settings.marketplace_index_url
    if not url:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no marketplace index configured (HOURSX_MARKETPLACE_INDEX_URL)",
        )
    index = await fetch_marketplace_index(url)
    return index.entries
