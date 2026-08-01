"""Plugin discovery.

Two sources, both explicit:

1. **Installed packages** exposing the ``hoursx.plugins`` entry-point group.
2. **Local plugin directory** (``HOURSX_PLUGIN_DIR``): each ``*.py`` file or
   package defining ``manifest()``.

A broken plugin never takes the platform down: its error is logged and it is
skipped, and the loaded/failed sets are reported so operators can see exactly
what is active.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
from dataclasses import dataclass, field
from pathlib import Path

from hoursx.observability import get_logger
from hoursx.sdk.manifest import PluginManifest

log = get_logger("sdk.discovery")

ENTRY_POINT_GROUP = "hoursx.plugins"


@dataclass
class DiscoveryReport:
    loaded: list[PluginManifest] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)  # source -> error


def load_local_plugin(path: Path) -> PluginManifest:
    """Import one local plugin file/package and return its manifest."""
    module_name = f"hoursx_local_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(
        module_name, path if path.is_file() else path / "__init__.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import plugin at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest_fn = getattr(module, "manifest", None)
    if not callable(manifest_fn):
        raise TypeError(f"plugin {path.name} does not define manifest()")
    manifest = manifest_fn()
    if not isinstance(manifest, PluginManifest):
        raise TypeError(f"plugin {path.name} manifest() must return PluginManifest")
    return manifest


def discover_plugins(plugin_dir: Path | None = None) -> DiscoveryReport:
    """Discover every available plugin from entry points and the local dir."""
    report = DiscoveryReport()

    for entry_point in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            manifest_fn = entry_point.load()
            manifest = manifest_fn()
            if not isinstance(manifest, PluginManifest):
                raise TypeError("entry point did not return PluginManifest")
            report.loaded.append(manifest)
        except Exception as exc:  # noqa: BLE001 — isolate plugin faults
            report.failed[f"entrypoint:{entry_point.name}"] = str(exc)
            log.warning("plugin entry point %s failed: %s", entry_point.name, exc)

    if plugin_dir and plugin_dir.is_dir():
        candidates = sorted(
            [
                *plugin_dir.glob("*.py"),
                *(p for p in plugin_dir.iterdir() if (p / "__init__.py").is_file()),
            ]
        )
        for path in candidates:
            try:
                report.loaded.append(load_local_plugin(path))
            except Exception as exc:  # noqa: BLE001 — isolate plugin faults
                report.failed[f"local:{path.name}"] = str(exc)
                log.warning("local plugin %s failed: %s", path.name, exc)

    return report
