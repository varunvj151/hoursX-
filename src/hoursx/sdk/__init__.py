"""HoursX Plugin SDK.

A plugin extends the tool surface. It is a Python package (or a module dropped
into the local plugin directory) exposing a ``manifest()`` callable that returns
a :class:`PluginManifest`. Plugins declare the permissions they need; operators
grant a subset at install time, and only tools covered by granted permissions
are registered. Discovery imports code — installation itself never executes
anything, and the marketplace index is inert JSON.
"""

from hoursx.sdk.discovery import discover_plugins, load_local_plugin
from hoursx.sdk.manifest import PluginManifest, PluginPermission, PluginTool
from hoursx.sdk.marketplace import MarketplaceEntry, MarketplaceIndex

__all__ = [
    "MarketplaceEntry",
    "MarketplaceIndex",
    "PluginManifest",
    "PluginPermission",
    "PluginTool",
    "discover_plugins",
    "load_local_plugin",
]
