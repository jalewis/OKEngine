"""Serper web search on Hermes v0.21.3's shared provider contract."""

from __future__ import annotations

import logging
from typing import Any, Dict

import httpx

from plugins.web._common import (
    BaseWebSearchProvider,
    provider_env,
    run_search,
    search_fail,
    search_ok,
    setup_schema,
    title_hit,
)

logger = logging.getLogger(__name__)

_SERPER_ENDPOINT = "https://google.serper.dev/search"


class SerperWebSearchProvider(BaseWebSearchProvider):
    """Search-only Google SERP provider; extraction stays with another backend."""

    NAME = "serper"
    DISPLAY_NAME = "Serper (Google)"
    KEY_ENV = "SERPER_API_KEY"

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        api_key = provider_env(self.KEY_ENV)
        if not api_key:
            return search_fail("SERPER_API_KEY is not set")

        def _search() -> Dict[str, Any]:
            requested = max(1, min(int(limit), 20))
            try:
                response = httpx.post(
                    _SERPER_ENDPOINT,
                    json={"q": query, "num": requested},
                    headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                    timeout=15,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.warning("Serper HTTP error: %s", exc)
                return search_fail(f"Serper returned HTTP {exc.response.status_code}")
            except httpx.RequestError as exc:
                logger.warning("Serper request error: %s", exc)
                return search_fail(f"Could not reach Serper: {exc}")
            try:
                data = response.json()
            except Exception as exc:  # noqa: BLE001 — malformed vendor response is a failure envelope
                logger.warning("Serper response parse error: %s", exc)
                return search_fail("Could not parse Serper response as JSON")
            if not isinstance(data, dict) or not isinstance(data.get("organic", []), list):
                return search_fail("Serper response has invalid organic results")
            raw_results = data.get("organic") or []
            if any(not isinstance(row, dict) for row in raw_results[:requested]):
                return search_fail("Serper response has invalid organic result rows")
            web_results = [
                title_hit(
                    str(row.get("title", "")),
                    str(row.get("link", "")),
                    str(row.get("snippet", "")),
                    row.get("position", index + 1),
                )
                for index, row in enumerate(raw_results[:requested])
            ]
            logger.info("Serper '%s': %d results (from %d raw, limit %d)",
                        query, len(web_results), len(raw_results), requested)
            return search_ok(web_results)

        return run_search("Serper", logger, _search)

    def get_setup_schema(self) -> Dict[str, Any]:
        return setup_schema(
            "Serper (Google)", "free", "Google SERP via serper.dev — free tier 2.5k queries, search only.",
            "SERPER_API_KEY", "Serper API key", "https://serper.dev",
        )
