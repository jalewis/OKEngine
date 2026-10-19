"""The engine's one frontmatter reader for generated views, selectors and audits (okengine#993).

Every reader here sees the page's WHOLE frontmatter envelope. Each consumer used to slice a
leading 4000- or 8000-character window first, and a merged page whose `status`, `published` or
`sources` sits below a long list parsed as `{}`: a retired actor stayed listed, a source vanished
from the daily brief, and a conformance audit reported fields that were present (okengine#991 audit
2 [2], okengine#993). The envelope is streamed line by line and reading stops at its closing
delimiter, so a page's body is never read: a 100k-page walk costs the frontmatter, not the corpus.

The placement drain (`okf_migrate`) keeps its own copy of this pattern because it ships in the
baked engine wheel. ``FRONTMATTER_RE`` is pinned to it by test, so a view and the drain agree on
what a page's frontmatter is.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

# Identical to okf_migrate._FM_RE (pinned by test). Pages are read in text mode, which turns
# CRLF into LF, so a CRLF page matches it too.
FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*(?:\n|\Z)", re.S)
_DELIMITER = re.compile(r"---[ \t]*")


def frontmatter_block(page: Path) -> str:
    """The raw text of the page's whole frontmatter envelope; '' when absent or unreadable.

    Streams the file and stops at the closing delimiter. The result is exactly
    ``FRONTMATTER_RE.match(page.read_text(...)).group(1)``, without reading the body."""
    try:
        with page.open(encoding="utf-8", errors="replace") as handle:
            opener = handle.readline()
            if not opener.endswith("\n") or not _DELIMITER.fullmatch(opener[:-1]):
                return ""
            lines: list[str] = []
            for line in handle:
                # The envelope holds at least one line, so a delimiter straight after the opener
                # is content, exactly as the regex's `(.*?\n)` requires.
                if lines and _DELIMITER.fullmatch(line.removesuffix("\n")):
                    return "".join(lines)
                lines.append(line)
    except OSError:
        return ""
    return ""


def load_frontmatter(block: str, loader=yaml.safe_load) -> dict:
    """Parse an envelope's text; {} when malformed or not a mapping.

    Any parse failure counts as unparseable, not just a YAMLError: an impossible date such as
    `2026-02-30` raises ValueError from YAML's timestamp constructor, and a generator must skip
    that page rather than crash the lane."""
    try:
        data = loader(block)
    except Exception:  # noqa: BLE001 - a YAMLError and an impossible date's ValueError alike
        return {}
    return data if isinstance(data, dict) else {}


def read_frontmatter(page: Path, loader=yaml.safe_load) -> dict:
    """The page's frontmatter as a dict; {} when absent, unreadable, malformed or not a mapping."""
    block = frontmatter_block(page)
    return load_frontmatter(block, loader) if block else {}
