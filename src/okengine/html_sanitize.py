"""Shared markdown->HTML sanitizer for the read surfaces (reader + cockpit).

Vault content is partly agent- and feed-derived and both UIs inject rendered HTML via
innerHTML, so everything that reaches the browser passes ONE nh3 allowlist. The reader has
always sanitized; the cockpit shipped ``nh3`` in its requirements but never called it
(okengine#659, stored XSS from any ingested page on a ``trust:public`` deployment). Keeping the
allowlist here, in the wheel both images install, is what stops the two UIs drifting apart
again.

The set covers what the markdown extensions (tables, fenced_code, sane_lists, nl2br) and the
UIs' own ``_linkify`` emit; everything else (inline <script>, event handlers, javascript: URLs,
...) is stripped by nh3.
"""
from __future__ import annotations

import re

import nh3

ALLOWED_TAGS: frozenset[str] = frozenset({
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr", "em", "strong", "b", "i",
    "code", "pre", "blockquote", "ul", "ol", "li", "a", "img", "span", "del",
    "table", "thead", "tbody", "tr", "th", "td",
    # inline charts (okengine.viz panel-svg blocks): static SVG shapes/text ONLY --
    # no script/foreignObject/animate/use/href, so nothing here can execute or fetch.
    "svg", "rect", "line", "circle", "text",
})
_SVG_PRESENTATION = {"fill", "stroke", "stroke-width", "stroke-dasharray",
                     "font-size", "font-weight", "font-style", "text-anchor", "opacity"}
ALLOWED_ATTRS: dict[str, set[str]] = {
    "a": {"href", "title", "class", "data-page", "target"},
    "img": {"src", "alt", "title"},
    "td": {"align"},
    "th": {"align"},
    "code": {"class"},
    "span": {"class"},
    "svg": {"xmlns", "viewBox", "width", "style"},
    "rect": {"x", "y", "width", "height"} | _SVG_PRESENTATION,
    "line": {"x1", "y1", "x2", "y2"} | _SVG_PRESENTATION,
    "circle": {"cx", "cy", "r"} | _SVG_PRESENTATION,
    "text": {"x", "y", "transform"} | _SVG_PRESENTATION,
}

# okengine.viz panel-svg blocks must bypass MARKDOWN (not the sanitizer): the nl2br extension
# injects <br/> between the shape lines, and <br> is an HTML5 foreign-content BREAKOUT tag -- a
# spec-following sanitizer parser (nh3/ammonia >= 0.3.6) closes the <svg> at the first <br> and
# every shape after it is silently dropped. Stash the blocks before markdown and re-insert them
# BEFORE nh3.clean, so the svg still gets the full allowlist pass.
PANEL_SVG_RE = re.compile(r"<!--\s*panel-svg\b.*?<!--\s*/panel-svg\s*-->", re.DOTALL)
_MARKER = "OKENGINEPANELSVG{}MARKER"


def stash_panel_svg(body: str) -> tuple[str, list[str]]:
    """Replace every panel-svg block with a marker; returns (body, stash)."""
    stash: list[str] = []

    def _stash(m: re.Match) -> str:
        stash.append(m.group(0))
        return _MARKER.format(len(stash) - 1)

    return PANEL_SVG_RE.sub(_stash, body), stash


def restore_panel_svg(html: str, stash: list[str]) -> str:
    for i, blk in enumerate(stash):
        html = html.replace(_MARKER.format(i), blk)
    return html


def sanitize(html: str) -> str:
    """The one call every read surface makes before HTML reaches a browser."""
    return nh3.clean(html, tags=set(ALLOWED_TAGS), attributes=ALLOWED_ATTRS)
