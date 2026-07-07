"""Serper web-search plugin — OKEngine overlay (okengine#190), auto-loaded via kind: backend.

Mirrors the bundled `plugins/web/brave_free/` layout: `provider.py` holds the provider class,
`__init__.py::register(ctx)` registers an instance into agent.web_search_registry.
"""

from __future__ import annotations

from plugins.web.serper.provider import SerperWebSearchProvider


def register(ctx) -> None:
    """Register the Serper provider with the plugin context."""
    ctx.register_web_search_provider(SerperWebSearchProvider())
