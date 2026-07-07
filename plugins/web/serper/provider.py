"""Serper (google.serper.dev) web-search provider — OKEngine overlay plugin (okengine#190).

Hermes' web tool ships no Serper backend. This adds one as an ENGINE OVERLAY (a new
`plugins/web/serper/` dir copied into the Hermes tree at image build — an addition, NOT a fork of
pinned Hermes), so Serper joins the native provider set + the `web.backend: rotate` rotation
(carried patch 08). Subclasses the plugin-facing ABC and normalizes Serper's Google-SERP response
onto the SAME envelope every provider returns — `{"success", "data": {"web": [...]}}` — so the
agent sees identical structure regardless of which backend answered (the normalization contract in
okengine#190).

Config keys::

    web:
      backend: "rotate"        # rotates across keyed backends incl. serper
      search_backend: "serper" # or pin serper explicitly

Auth env var::

    SERPER_API_KEY=...    # https://serper.dev (free tier: 2,500 queries)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

_SERPER_ENDPOINT = "https://google.serper.dev/search"


class SerperWebSearchProvider(WebSearchProvider):
    """Search-only Serper provider (Google SERP via serper.dev). No content extraction —
    pair with Firecrawl/Tavily/Exa for ``web_extract``."""

    @property
    def name(self) -> str:
        return "serper"

    @property
    def display_name(self) -> str:
        return "Serper (Google)"

    def is_available(self) -> bool:
        """True when ``SERPER_API_KEY`` is set to a non-empty value."""
        return bool(os.getenv("SERPER_API_KEY", "").strip())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Search via serper.dev. Returns the common envelope
        ``{"success": True, "data": {"web": [{"title", "url", "description", "position"}]}}`` on
        success, or ``{"success": False, "error": str}`` on failure."""
        import httpx

        api_key = os.getenv("SERPER_API_KEY", "").strip()
        if not api_key:
            return {"success": False, "error": "SERPER_API_KEY is not set"}

        num = max(1, min(int(limit), 20))

        try:
            resp = httpx.post(
                _SERPER_ENDPOINT,
                json={"q": query, "num": num},
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                timeout=15,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("Serper HTTP error: %s", exc)
            return {"success": False, "error": f"Serper returned HTTP {exc.response.status_code}"}
        except httpx.RequestError as exc:
            logger.warning("Serper request error: %s", exc)
            return {"success": False, "error": f"Could not reach Serper: {exc}"}

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Serper response parse error: %s", exc)
            return {"success": False, "error": "Could not parse Serper response as JSON"}

        raw_results = data.get("organic", []) or []
        truncated = raw_results[:limit]

        web_results = [
            {
                "title": str(r.get("title", "")),
                "url": str(r.get("link", "")),          # Serper uses `link`, not `url`
                "description": str(r.get("snippet", "")),  # Serper uses `snippet`, not `content`
                "position": r.get("position", i + 1),
            }
            for i, r in enumerate(truncated)
        ]

        logger.info("Serper '%s': %d results (from %d raw, limit %d)",
                    query, len(web_results), len(raw_results), limit)

        return {"success": True, "data": {"web": web_results}}

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Serper (Google)",
            "badge": "free",
            "tag": "Google SERP via serper.dev — free tier 2.5k queries, search only.",
            "env_vars": [
                {
                    "key": "SERPER_API_KEY",
                    "prompt": "Serper API key",
                    "url": "https://serper.dev",
                },
            ],
        }
