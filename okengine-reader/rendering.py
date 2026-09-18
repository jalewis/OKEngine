"""Markdown, wikilink, embed, and source-link rendering service."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlparse

import markdown as md
from okengine.html_sanitize import (ALLOWED_ATTRS, ALLOWED_TAGS, PANEL_SVG_RE,
                                    restore_panel_svg, sanitize, stash_panel_svg)
import yaml

WIKI: Path | None = None
_READ_HEAD: Callable[[Path], str] | None = None
_EXCLUDED_DIRS: Callable[[], frozenset[str]] | None = None
_NS_DIRS: Callable[[Path], frozenset[str]] | None = None
_WITHIN: Callable[[Path, Path], bool] | None = None


def configure(
    wiki: Path,
    read_head: Callable[[Path], str],
    excluded_dirs: Callable[[], frozenset[str]],
    ns_dirs: Callable[[Path], frozenset[str]],
    within: Callable[[Path, Path], bool],
) -> None:
    global WIKI, _READ_HEAD, _EXCLUDED_DIRS, _NS_DIRS, _WITHIN
    if WIKI != wiki:
        WIKI = wiki
        _LINK_TITLE_CACHE.clear()
        _EMBED_PATH_CACHE.clear()
    _READ_HEAD = read_head
    _EXCLUDED_DIRS = excluded_dirs
    _NS_DIRS = ns_dirs
    _WITHIN = within


_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_WIKILINK = re.compile(r"\[\[\s*([^\]|#\n\\]*)(?:#([^\]|\n]+))?(?:\\?\|\s*([^\]\n]+?))?\s*\]\]")
_EMBED = re.compile(r"!\[\[\s*([^\]\n#|]+?)\s*(?:#[^\]\n|]+)?(?:\|[^\]\n]+)?\s*\]\]")
_H1_RE = re.compile(r"^#\s+.*$", re.MULTILINE)


def _skip(name: str) -> bool:
    """Reserved / non-content / generated files the reader never lists or renders
    (underscore/dot reserved, backups, and the generated per-directory index pages
    build_index_tree/rebuild_index emit — `INDEX.md` / `INDEX-pNN.md` / `index.md`)."""
    return (name.startswith(("_", ".")) or ".bak." in name
            or name in ("INDEX.md", "index.md")
            or name.startswith(("INDEX-", "index-")))


def _within(base: Path, p: Path) -> bool:
    """True iff `p` (already resolved) is inside `base` — path-traversal guard."""
    try:
        p.relative_to(base.resolve())
        return True
    except ValueError:
        return False


try:
    # libyaml — ~7x faster frontmatter parsing, which is the dominant cost of the
    # full-vault scan behind the BY KIND counts. Falls back to the pure-Python loader
    # (identical semantics) if the C extension isn't built into PyYAML.
    from yaml import CSafeLoader as _YAML_LOADER
except ImportError:  # pragma: no cover - libyaml absent in a minimal PyYAML build
    from yaml import SafeLoader as _YAML_LOADER


def split_fm(text: str) -> tuple[dict, str]:
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.load(m.group(1), Loader=_YAML_LOADER) or {}  # nosec B506 - SafeLoader/CSafeLoader
    except yaml.YAMLError:
        fm = {}
    return (fm if isinstance(fm, dict) else {}), m.group(2)


_LINK_TITLE_CACHE: dict = {}


def _link_title(target: str) -> str | None:
    """The target page's human title (frontmatter `title`/`name`) for friendlier wikilink text —
    so a citation reads 'EvilTokens: a phishing attack…' instead of the raw slug
    `eviltokens-a-phishing-attack…`. Resolves a flat-form target to its sharded page (basename).
    Returns None when there's no real title (caller falls back to the slug). Cached for the
    process lifetime (titles are stable; the reader restarts on deploy)."""
    target = (target or "").strip()
    if not target or "://" in target:
        return None
    if target in _LINK_TITLE_CACHE:
        return _LINK_TITLE_CACHE[target]
    title = None
    key = target[:-3] if target.endswith(".md") else target
    try:
        cand = (WIKI / (key + ".md")).resolve()
        hit = cand if (cand.is_file() and _WITHIN(WIKI, cand)) else None
        if hit is None:
            hit = _resolve_basename(Path(key).name + ".md")
        if hit is not None:
            fm, _ = split_fm(_READ_HEAD(hit))
            title = (str(fm.get("title") or fm.get("name") or "").strip()) or None
    except OSError:
        pass
    _LINK_TITLE_CACHE[target] = title
    return title


def _wl_display(m) -> str:
    """Display text for a wikilink: alias, else the target page's title, else its last segment."""
    alias = (m.group(3) or "").strip()
    if alias:
        return alias
    target = (m.group(1) or "").strip()
    if target:
        return _link_title(target) or target.split("/")[-1]
    return (m.group(2) or "").strip()


def _delink(s: str) -> str:
    """Render a wikilink as plain display text (for portable markdown export)."""
    return _WIKILINK.sub(_wl_display, s)


# `[APT41](entities/a/apt41)` — an INTERNAL vault link (the agent's linked-title citations). Not an
# image (`!` excluded), not external (http/mailto/# excluded).
_MD_LOCAL_LINK = re.compile(r"(?<!\!)\[([^\]\n]+)\]\((?!https?://|mailto:|#)[^)\n]*\)")


def _deref_local_links(s: str) -> str:
    """Flatten internal markdown links to their text for portable export — `[APT41](entities/a/apt41)`
    -> `APT41`. External http(s)/mailto links are kept. Vault paths resolve only inside the reader,
    so an exported md/docx/pdf must not carry them as dead links."""
    return _MD_LOCAL_LINK.sub(r"\1", s)


_EMBED_PATH_CACHE: dict = {}


def _resolve_basename(name: str) -> "Path | None":
    """Resolve a bare basename (`slug.md`) to its CANONICAL page, exactly as _resolve_page does: skip
    generated/reserved files, then on a multi-hit DROP schema-excluded namespaces and PREFER the
    entities/ page (a multi-source entity also has observations/<src>/… copies with the same slug),
    then require uniqueness. Shared by the embed + link-title resolvers so all three agree — a naive
    len==1 gate rendered a multi-source entity as "missing" (invariant-audit #16 / L7)."""
    if not WIKI.is_dir():
        return None
    hits = [p for p in WIKI.rglob(name) if not _skip(p.name)]
    if len(hits) > 1:
        excl = _EXCLUDED_DIRS()
        pref = [p for p in hits if not (_NS_DIRS(p) & excl)] or hits
        ent = [p for p in pref if "entities" in _NS_DIRS(p)]
        hits = ent or pref
    return hits[0] if len(hits) == 1 else None


def _embed_rglob(name: str) -> "Path | None":
    """Canonical vault match for a basename `name` (`…​.md`), memoized for the process lifetime
    (mirrors `_LINK_TITLE_CACHE`). Basename embeds ![[apt29]] are the norm on sharded OKF
    vaults, so without this each render re-walks the WHOLE tree once per unresolved embed."""
    if name in _EMBED_PATH_CACHE:
        return _EMBED_PATH_CACHE[name]
    hit = _resolve_basename(name)
    _EMBED_PATH_CACHE[name] = hit
    return hit


def _resolve_embeds(text: str, depth: int = 0) -> str:
    """Inline Obsidian embeds ![[target]] with the target file's body, recursively
    (depth-limited). Targets are resolved anywhere under the vault, so this works
    on any pack's layout."""
    if depth > 3:
        return text

    def repl(mo: "re.Match") -> str:
        target = mo.group(1).strip()
        if re.search(r"\.(png|jpe?g|gif|svg|webp|pdf)$", target, re.I):
            return f"_[embedded asset: {target}]_"
        cand = WIKI / (target + ".md")
        if not cand.is_file():
            cand = _embed_rglob(Path(target).name + ".md")
        if not cand:
            return f"_[missing embed: {target}]_"
        try:
            cp = cand.resolve()
            if not _WITHIN(WIKI, cp):
                return "_[blocked embed]_"
            _, body = split_fm(cp.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return f"_[unreadable embed: {target}]_"
        return _resolve_embeds(body, depth + 1)

    return _EMBED.sub(repl, text)


def _linkify(s: str) -> str:
    """Wikilinks -> clickable anchors (resolved client-side via /api/page)."""
    def repl(m: "re.Match") -> str:
        target = (m.group(1) or "").strip()
        disp = _wl_display(m)
        if not target:                       # same-page anchor [[#heading]] — no page
            return disp
        return f'<a class="wl" data-page="{target.replace(chr(34), "&quot;")}">{disp}</a>'
    s = _WIKILINK.sub(repl, s)
    # drop dangling "[[" from truncated wikilinks
    return re.sub(r"\[\[(?![^\]\n]*\]\])", "", s)


# Sanitizer allowlist + panel-svg stash live in the wheel (okengine.html_sanitize) and are
# SHARED with the cockpit (okengine#659): one allowlist, two UIs, no drift. The private names
# below are kept as aliases for existing callers/tests.
_ALLOWED_TAGS = ALLOWED_TAGS
_ALLOWED_ATTRS = ALLOWED_ATTRS
_PANEL_SVG_RE = PANEL_SVG_RE


# Agents across lanes "highlight" a wikilink by wrapping it in backticks (`[[x]]`). That makes
# _linkify inject the <a> INSIDE an inline-code span, so markdown escapes it to visible `<a …>` text
# in the UI. Strip the backticks around a bare wikilink first — the author meant a link, not code.
_UNCODE_WIKILINK = re.compile(r"`(\[\[[^`]+?\]\])`")


def _uncode_wikilinks(s: str) -> str:
    return _UNCODE_WIKILINK.sub(r"\1", s)


def render_md(body: str) -> str:
    body = _resolve_embeds(body)
    body = re.sub(r"```dataview(js)?\n.*?\n```",
                  "_[Dataview view — open in Obsidian to compute]_", body, flags=re.DOTALL)
    body, stash = stash_panel_svg(body)
    body = _uncode_wikilinks(body)
    body = _linkify(body)
    html = md.markdown(body, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])
    html = restore_panel_svg(html, stash)
    return sanitize(_link_originals(html))


_SRC_WL = re.compile(r'<a class="wl" data-page="(sources/[^"]+)"[^>]*>([^<]*)</a>')


def _link_originals(html: str) -> str:
    """A cited source page carries the ORIGINAL article's `url:` in its frontmatter. Promote the
    citation so its TITLE links STRAIGHT to that article — one click reaches the primary
    reporting, instead of the title pointing at the internal source stub with the real url
    demoted to a small glyph. Falls back to the internal wikilink only when the source has no
    http(s) `url:`. Runs BEFORE nh3.clean: the anchor uses allowlisted attrs (href/target/class)
    and nh3 stamps rel=noopener itself. Mirrors the cockpit's treatment — both UIs, same
    affordance."""
    def _add(m):
        whole, rel, text = m.group(0), m.group(1), m.group(2)
        try:
            fp = (WIKI / (rel + ".md")).resolve()
            if not _within(WIKI, fp) or not fp.is_file():
                return whole
            fm_m = re.match(r"\A---\s*\n(.*?\n)---", fp.read_text(encoding="utf-8", errors="replace"), re.S)
            u = ""
            if fm_m:
                um = re.search(r"^url:\s*(\S+)", fm_m.group(1), re.M)
                if um:
                    u = um.group(1).strip("'\"")
        except OSError:
            return whole
        if u.startswith(("http://", "https://")):
            return (f'<a class="ext" href="{u}" target="_blank" '
                    f'title="original article">{text}</a>')
        return whole
    return _SRC_WL.sub(_add, html)


# ── browse: discover the vault structure at runtime ─────────────────────────
