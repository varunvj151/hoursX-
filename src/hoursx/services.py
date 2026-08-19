"""Application service container.

One explicit wiring point: every process (API, worker, CLI, tests) builds an
:class:`AppServices` and passes it down. No module-level singletons beyond the
container itself, so tests can assemble any combination of real and fake parts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hoursx.channels.router import ChannelRegistry, build_registry
from hoursx.config import HoursXSettings, get_settings
from hoursx.db.engine import Database
from hoursx.events import EventBus, build_event_bus
from hoursx.knowledge import KnowledgeEngine
from hoursx.memory import MemoryManager
from hoursx.observability import get_logger
from hoursx.providers.router import ModelRouter, build_default_router
from hoursx.sdk.discovery import discover_plugins
from hoursx.sdk.manifest import PluginPermission
from hoursx.tools.builtin import register_builtin_tools
from hoursx.tools.executor import ToolExecutor
from hoursx.tools.registry import ToolRegistry
from hoursx.tools.mcp_adapter import MCPAdapter

log = get_logger("services")


@dataclass
class AppServices:
    settings: HoursXSettings
    db: Database
    bus: EventBus
    router: ModelRouter
    memory: MemoryManager
    knowledge: KnowledgeEngine
    registry: ToolRegistry
    executor: ToolExecutor
    channels: ChannelRegistry
    mcp_adapter: MCPAdapter | None = None

    def sandbox_root(self) -> Path:
        root = Path(self.settings.workspace_root)
        root.mkdir(parents=True, exist_ok=True)
        return root

    async def start(self) -> None:
        if self.mcp_adapter:
            tools = await self.mcp_adapter.start()
            for t in tools:
                self.registry.register(t)

    async def stop(self) -> None:
        if self.mcp_adapter:
            await self.mcp_adapter.stop()


def build_services(
    settings: HoursXSettings | None = None,
    *,
    db: Database | None = None,
    router: ModelRouter | None = None,
    bus: EventBus | None = None,
) -> AppServices:
    """Assemble the service graph; every part is overridable for tests."""
    settings = settings or get_settings()
    db = db or Database(settings.database_url)
    bus = bus or build_event_bus(settings.redis_url)
    router = router or build_default_router(settings)

    registry = ToolRegistry()
    register_builtin_tools(registry)
    _register_plugin_tools(registry, settings)

    return AppServices(
        settings=settings,
        db=db,
        bus=bus,
        router=router,
        memory=MemoryManager(router),
        knowledge=KnowledgeEngine(router, settings.chunk_size_chars, settings.chunk_overlap_chars),
        registry=registry,
        executor=ToolExecutor(registry, default_timeout=settings.tool_timeout_seconds),
        channels=build_registry(settings),
        mcp_adapter=MCPAdapter(settings.mcp_servers) if settings.mcp_servers else None,
    )


def _register_plugin_tools(registry: ToolRegistry, settings: HoursXSettings) -> None:
    """Register discovered plugin tools. Discovery grants every permission a
    manifest requests for locally-dropped plugins (the operator placed the file,
    which is the grant); marketplace installs are narrowed by the recorded grant
    at the API layer."""
    report = discover_plugins(Path(settings.plugin_dir))
    for manifest in report.loaded:
        for tool in manifest.granted_tools(set(PluginPermission)):
            try:
                registry.register(tool)
            except ValueError as exc:
                log.warning("plugin %s: %s", manifest.name, exc)
    for source, error in report.failed.items():
        log.warning("plugin %s failed to load: %s", source, error)
