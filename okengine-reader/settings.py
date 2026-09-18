"""Environment-backed reader deployment settings."""
from __future__ import annotations

import os
import threading

import chat
import limits

PUBLIC = limits.flag("OKENGINE_READER_PUBLIC", False)
EXPORTS_ENABLED = limits.flag("OKENGINE_READER_EXPORTS", not PUBLIC)
EXPORT_SEMAPHORE = threading.BoundedSemaphore(
    limits.intenv("OKENGINE_READER_MAX_EXPORT", 2, lo=1)
)
SEARCH_SEMAPHORE = threading.BoundedSemaphore(
    limits.intenv("OKENGINE_READER_MAX_SEARCH", 4, lo=1)
)
RATE_LIMITER = limits.RateLimiter(
    limits.intenv("OKENGINE_READER_RATE", 60 if PUBLIC else 300, lo=0)
)

AGENT_API = os.environ.get("OKENGINE_AGENT_API", "").rstrip("/")
AGENT_KEY = os.environ.get("OKENGINE_AGENT_KEY", "")
AGENT_MODEL = os.environ.get("OKENGINE_AGENT_MODEL", "OKEngine Agent")
CHAT_MAX_MESSAGES = limits.intenv("OKENGINE_READER_CHAT_MAX_MSGS", 24, lo=2)
CHAT_MAX_CHARACTERS = limits.intenv("OKENGINE_READER_CHAT_MAX_CHARS", 8000, lo=200)
AGENT_SYSTEM = os.environ.get("OKENGINE_AGENT_SYSTEM") or chat.DEFAULT_AGENT_SYSTEM

def editing_enabled() -> bool:
    return os.environ.get("OKENGINE_EDITING", "").strip().lower() not in (
        "0", "false", "no", "off",
    )
