"""Register OKEngine's Serper backend through Hermes v0.21.3's plugin context."""

from __future__ import annotations

from plugins.web.serper.provider import SerperWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(SerperWebSearchProvider())
