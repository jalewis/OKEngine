#!/usr/bin/env python3
"""panel_svg.py — okengine.viz: server-side SVG for `panel: {kind: two-axis}` data.

The origin-system lesson (its wardley_map_refresh.py): render the chart INTO the page
body as inline SVG, so it shows anywhere markdown renders — every UI tab, every
route, exports — with zero client-side code and zero cache sensitivity. The client
two-axis renderer stays as a fallback for panels without an embedded block; the
read surfaces suppress it when the body carries one (the marker below).

Self-contained styling (own background, fixed palette) so the chart renders
identically on light/dark UIs and outside them.
"""
from __future__ import annotations

import hashlib
import json

# bump when the SVG output changes: it feeds the block hash, so existing embedded
# blocks refresh on the next drain pass even when the panel DATA is unchanged.
_RENDERER_REV = 2

MARK_OPEN = "<!-- panel-svg"          # full form: <!-- panel-svg v=<hash8> -->
MARK_CLOSE = "<!-- /panel-svg -->"

_C = {"bg": "#fafafa", "band": "#f3f4f6", "frame": "#374151", "grid": "#d1d5db",
      "text": "#111827", "dim": "#6b7280", "dot": "#1d4ed8", "edge": "#9ca3af"}


def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _num(v) -> float:
    """Coerce a coordinate to a float, defaulting to 0.0. The panel is agent-authored,
    so a node/band can carry a non-numeric x/y ("high", None, a list). float() would raise
    and crash the whole refresh lane; here it degrades to the axis origin (invariant-audit B6.1)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _hash_default(o):
    """Deterministic JSON fallback for panel values yaml.safe_load yields that json can't serialize:
    a bare ISO date -> datetime.date, a !!set -> set. Sets are SORTED, not str(set) — str(set)'s
    order is PYTHONHASHSEED-dependent, so hashing it would change every process run and rewrite the
    page's SVG block on every refresh (perpetual churn, violating the idempotent contract). Bare
    dates were already deterministic; this pins the set case too (invariant-audit B6.1 re-verify)."""
    if isinstance(o, (set, frozenset)):
        return sorted(map(str, o))
    return str(o)


def panel_hash(panel: dict) -> str:
    # The panel comes from yaml.safe_load of page frontmatter, which turns bare ISO dates into
    # datetime.date and !!set into set — both non-JSON-serializable. Without a default, json.dumps
    # raises TypeError on a perfectly VALID panel carrying an `as_of: 2026-07-10` field, and (since
    # this runs after render) the exception escaped svg_block's render-only guard and aborted the
    # whole panel-svg refresh lane. _hash_default serializes them DETERMINISTICALLY (invariant-audit B6.1).
    return hashlib.sha1(f"r{_RENDERER_REV}:".encode()
                        + json.dumps(panel, sort_keys=True, default=_hash_default).encode()).hexdigest()[:8]


def render_panel_svg(panel: dict) -> str | None:
    """A two-axis panel -> inline SVG (None for other kinds). Bands become tinted
    columns with labels; edges draw beneath the dots; labels get per-column slot
    spacing (the origin-system anti-overlap approach — vertical slots, no horizontal
    jitter: labels are wide, short horizontal offsets always collide)."""
    if not isinstance(panel, dict) or panel.get("kind") != "two-axis":
        return None
    W, H = 820, 520
    ML, MR, MT, MB = 50, 30, 46, 56
    PW, PH = W - ML - MR, H - MT - MB
    px = lambda v: ML + max(0.0, min(1.0, _num(v))) * PW          # noqa: E731
    py = lambda v: MT + PH - max(0.0, min(1.0, _num(v))) * PH     # noqa: E731
    # agent-authored — keep only well-shaped entries so one bad node/band/edge can't crash the
    # lane. nodes/bands must be dicts; an edge is any 2+-element sequence of slugs (invariant-audit B6.1).
    nodes = [n for n in (panel.get("nodes") or []) if isinstance(n, dict)]
    bands = [b for b in (panel.get("x_bands") or []) if isinstance(b, dict)]
    edges = [e for e in (panel.get("edges") or []) if isinstance(e, (list, tuple)) and len(e) >= 2]

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="100%" '
             f'style="max-width:{W}px;font-family:system-ui,-apple-system,sans-serif;">',
             f'<rect width="{W}" height="{H}" fill="{_C["bg"]}"/>']
    # bands: alternating tint + dashed divider + label under the axis
    for i, b in enumerate(bands):
        x0, x1 = px(b.get("from", 0)), px(b.get("to", 1))
        if i % 2:
            parts.append(f'<rect x="{x0:.1f}" y="{MT}" width="{x1 - x0:.1f}" height="{PH}" fill="{_C["band"]}"/>')
        if _num(b.get("from")) > 0:
            parts.append(f'<line x1="{x0:.1f}" y1="{MT}" x2="{x0:.1f}" y2="{MT + PH}" '
                         f'stroke="{_C["grid"]}" stroke-width="0.8" stroke-dasharray="3,3"/>')
        parts.append(f'<text x="{(x0 + x1) / 2:.1f}" y="{MT + PH + 16}" text-anchor="middle" '
                     f'font-size="12" fill="{_C["text"]}" font-weight="600">{_esc(b.get("label"))}</text>')
    parts.append(f'<rect x="{ML}" y="{MT}" width="{PW}" height="{PH}" fill="none" '
                 f'stroke="{_C["frame"]}" stroke-width="1.5"/>')
    # axis titles
    parts.append(f'<text x="{ML + PW}" y="{MT + PH + 36}" text-anchor="end" font-size="11" '
                 f'fill="{_C["dim"]}" font-style="italic">{_esc(panel.get("x_label"))}</text>')
    parts.append(f'<text x="18" y="{MT + PH / 2:.1f}" text-anchor="middle" font-size="11" fill="{_C["dim"]}" '
                 f'font-style="italic" transform="rotate(-90 18 {MT + PH / 2:.1f})">{_esc(panel.get("y_label"))}</text>')
    # edges beneath the dots
    by_slug = {n.get("slug"): n for n in nodes if n.get("slug")}
    for e in edges:
        if len(e) < 2:
            continue
        a, b = by_slug.get(e[0]), by_slug.get(e[1])
        if a and b:
            parts.append(f'<line x1="{px(a.get("x")):.1f}" y1="{py(a.get("y")):.1f}" x2="{px(b.get("x")):.1f}" '
                         f'y2="{py(b.get("y")):.1f}" stroke="{_C["edge"]}" stroke-width="1"/>')
    # label slotting: within loose x-columns, spread labels that would overlap vertically
    laid = []
    for n in sorted(nodes, key=lambda n: (_num(n.get("x")), _num(n.get("y")))):
        cx, cy = px(n.get("x")), py(n.get("y"))
        ly = cy
        for ox, oy in laid:
            if abs(ox - cx) < 190 and abs(oy - ly) < 14:
                ly = oy + 14
        laid.append((cx, ly))
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{_C["dot"]}"/>')
        if abs(ly - cy) > 1:   # nudged label: a leader line keeps it attached to its dot
            parts.append(f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{cx + 7:.1f}" y2="{ly - 4:.1f}" '
                         f'stroke="{_C["grid"]}" stroke-width="0.6"/>')
        parts.append(f'<text x="{cx + 9:.1f}" y="{ly + 4:.1f}" font-size="11" '
                     f'fill="{_C["text"]}">{_esc(n.get("label"))}</text>')
    parts.append("</svg>")
    # ONE line: python-markdown doesn't know <svg> as a block-level element, so a
    # multi-line SVG gets its interior paragraph-split out of the wrapper (the
    # collapsed-chart bug). A single line gives markdown nothing to split.
    return "".join(parts)


def svg_block(panel: dict) -> str | None:
    """The marker-wrapped block embedded in a page body. The hash lets a refresh
    lane skip pages whose panel data hasn't changed."""
    # The field-level coercions above cover the known bad shapes; this outer guard is the
    # lane's last line of defense — an unforeseen shape skips ONE page's panel instead of
    # aborting the whole refresh lane mid-sweep. It MUST wrap panel_hash too: that runs after
    # render and json.dumps'd the raw panel, so a non-serializable field escaped a render-only
    # guard and crashed the lane (invariant-audit B6.1 re-verify).
    try:
        svg = render_panel_svg(panel)
        if svg is None:
            return None
        return f"{MARK_OPEN} v={panel_hash(panel)} -->\n{svg}\n{MARK_CLOSE}"
    except Exception:
        return None


def upsert_block(body: str, panel: dict) -> str | None:
    """body with the SVG block inserted (after a leading H1 if present) or replaced.
    Returns None when the body is already current (same panel hash) or the panel
    isn't renderable."""
    block = svg_block(panel)
    if block is None:
        return None
    if MARK_OPEN in body:
        if f"v={panel_hash(panel)}" in body.split(MARK_OPEN, 1)[1][:40]:
            return None                                    # already current
        pre = body.split(MARK_OPEN, 1)[0]
        post = body.split(MARK_CLOSE, 1)[1] if MARK_CLOSE in body else ""
        return f"{pre}{block}{post}"
    lines = body.split("\n")
    at = next((i + 1 for i, l in enumerate(lines[:10]) if l.startswith("# ")), 0)
    return "\n".join(lines[:at] + ["", block, ""] + lines[at:])
