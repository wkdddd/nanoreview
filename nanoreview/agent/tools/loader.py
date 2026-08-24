"""Tool discovery and registration via package scanning."""
from __future__ import annotations

import importlib
import pkgutil
from importlib.metadata import entry_points
from typing import Any

from loguru import logger

from nanoreview.agent.tools.base import Tool
from nanoreview.agent.tools.registry import ToolRegistry

_SKIP_MODULES = frozenset({
    "base", "schema", "registry", "context", "loader", "config",
    "file_state", "sandbox", "mcp", "__init__", "runtime_state",
    "review_base",
})


class ToolLoader:
    def __init__(self, package: Any = None, *, test_classes: list[type[Tool]] | None = None):
        if package is None:
            import nanoreview.agent.tools as _pkg
            package = _pkg
        self._package = package
        self._test_classes = test_classes
        self._discovered: list[type[Tool]] | None = None
        self._plugins: dict[str, type[Tool]] | None = None

    def discover(self) -> list[type[Tool]]:
        if self._test_classes is not None:
            return list(self._test_classes)
        if self._discovered is not None:
            return self._discovered
        seen: set[int] = set()
        results: list[type[Tool]] = []
        prefix = f"{self._package.__name__}."
        for _importer, module_name, _ispkg in pkgutil.walk_packages(self._package.__path__, prefix):
            short_name = module_name.removeprefix(prefix)
            root_name = short_name.split(".", 1)[0]
            leaf_name = short_name.rsplit(".", 1)[-1]
            if "." not in short_name and (root_name.startswith("_") or root_name in _SKIP_MODULES):
                continue
            if leaf_name in _SKIP_MODULES or leaf_name.startswith("__"):
                continue
            try:
                module = importlib.import_module(module_name)
            except Exception:
                logger.exception("Failed to import tool module: {}", module_name)
                continue
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, Tool)
                    and attr is not Tool
                    and not attr_name.startswith("_")
                    and not getattr(attr, "__abstractmethods__", None)
                    and getattr(attr, "_plugin_discoverable", True)
                    and id(attr) not in seen
                ):
                    seen.add(id(attr))
                    results.append(attr)
        results.sort(key=lambda cls: cls.__name__)
        self._discovered = results
        return results

    def _discover_plugins(self) -> dict[str, type[Tool]]:
        """Discover external tool plugins registered via entry_points."""
        if self._plugins is not None:
            return self._plugins
        plugins: dict[str, type[Tool]] = {}
        try:
            eps = entry_points(group="nanoreview.tools")
        except Exception:
            return plugins
        for ep in eps:
            try:
                cls = ep.load()
                if (
                    isinstance(cls, type)
                    and issubclass(cls, Tool)
                    and not getattr(cls, "__abstractmethods__", None)
                    and getattr(cls, "_plugin_discoverable", True)
                ):
                    plugins[ep.name] = cls
            except Exception:
                logger.exception("Failed to load tool plugin: {}", ep.name)
        self._plugins = plugins
        return plugins

    def load(
        self,
        ctx: Any,
        registry: ToolRegistry,
        *,
        scope: str = "core",
        allowed_names: frozenset[str] | set[str] | None = None,
    ) -> list[str]:
        registered: list[str] = []
        builtin_names: set[str] = set()
        sources = [(self.discover(), False), (self._discover_plugins().values(), True)]
        for source, is_plugin_source in sources:
            for tool_cls in source:
                cls_label = tool_cls.__name__
                try:
                    if scope not in getattr(tool_cls, "_scopes", {"core"}):
                        continue
                    if not tool_cls.enabled(ctx):
                        continue
                    tool = tool_cls.create(ctx)
                    if allowed_names is not None and tool.name not in allowed_names:
                        continue
                    if registry.has(tool.name):
                        if is_plugin_source and tool.name in builtin_names:
                            logger.warning(
                                "Plugin {} skipped: conflicts with built-in tool {}",
                                cls_label, tool.name,
                            )
                            continue
                        logger.warning(
                            "Tool name collision: {} from {} overwrites existing",
                            tool.name, cls_label,
                        )
                    registry.register(tool)
                    registered.append(tool.name)
                    if not is_plugin_source:
                        builtin_names.add(tool.name)
                except Exception:
                    logger.exception("Failed to register tool: {}", cls_label)
        return registered
