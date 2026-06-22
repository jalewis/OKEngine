#!/usr/bin/env python3
"""Stage-1 mechanical extraction: turn raw HTML pages into clean article text.

Parallels scripts/extract-pdfs.sh — each `foo.html` gets a `foo.html.txt`
companion of the extracted main-article text, which the raw-backfill selector
(scripts/cron/select_raw_batch.py) prefers over the noisy raw HTML so the ingest
agent reads the article, not nav/ads/boilerplate. Domain-agnostic: it knows
nothing but "HTML in, article text out".

Backend (best available, degrades gracefully — no hard dependency):
  1. trafilatura      (pip install trafilatura)   — best accuracy
  2. readability-lxml (pip install readability-lxml)
  3. a built-in stdlib heuristic (always available; lower accuracy)
For a site the generic pass gets wrong, pass `--selector "<css>"` to pull a
specific element (needs lxml+cssselect), or run per-site batches.

Idempotent: skips an HTML file whose `.html.txt` companion is newer. Output under
`--min-chars` (default 200) is treated as a failed extraction (boilerplate-only or
JS-rendered) — flagged, no companion written, so it stays in the queue.

Usage:
  python scripts/extract-html.py                  # default: $WIKI_PATH/raw (else /opt/vault/raw)
  python scripts/extract-html.py /path/to/raw
  python scripts/extract-html.py --dry-run [root]
  python scripts/extract-html.py --selector ".article-body" [root]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

# Tags whose text is never article content.
_DROP_TAGS = {"head", "script", "style", "noscript", "nav", "header", "footer",
              "aside", "form", "button", "svg", "iframe", "figure", "figcaption",
              "template"}
# Block-level tags that force a line break between text runs.
_BLOCK_TAGS = {"p", "div", "section", "article", "main", "li", "ul", "ol", "br",
               "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote", "pre", "td"}


class _Heuristic(HTMLParser):
    """Strip boilerplate tags, prefer text inside <article>/<main>, break on
    block tags. If no article zone exists, keep sentence-like lines and drop
    short link-y nav lines."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._drop = 0
        self._in_article = 0
        self._chunks: list[tuple[bool, str]] = []
        self._cur: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_TAGS:
            self._drop += 1
        if tag in ("article", "main"):
            self._in_article += 1
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in _DROP_TAGS and self._drop:
            self._drop -= 1
        if tag in ("article", "main") and self._in_article:
            self._in_article -= 1
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if self._drop == 0:
            t = data.strip()
            if t:
                self._cur.append(t)

    def _flush(self):
        if self._cur:
            line = " ".join(self._cur).strip()
            if line:
                self._chunks.append((self._in_article > 0, line))
            self._cur = []

    def result(self) -> str:
        self._flush()
        article = [ln for in_art, ln in self._chunks if in_art]
        if article:
            return "\n".join(article)
        # No <article>/<main>: keep sentence-ish lines, drop short nav links.
        return "\n".join(ln for _, ln in self._chunks
                         if len(ln) >= 40 or ln.rstrip().endswith((".", "!", "?", ":")))


def _heuristic_extract(html: str) -> str:
    p = _Heuristic()
    try:
        p.feed(html)
    except Exception:
        pass
    return re.sub(r"\n{3,}", "\n\n", p.result()).strip()


def _try_trafilatura(html: str) -> str | None:
    try:
        import trafilatura
        return trafilatura.extract(html, include_comments=False,
                                   include_tables=True, favor_recall=True) or None
    except Exception:
        return None


def _try_readability(html: str) -> str | None:
    try:
        from readability import Document
        return _heuristic_extract(Document(html).summary()) or None
    except Exception:
        return None


def _by_selector(html: str, css: str) -> str | None:
    try:
        import lxml.html
        nodes = lxml.html.fromstring(html).cssselect(css)
        text = "\n".join(n.text_content() for n in nodes).strip()
        return re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", text)) or None
    except Exception:
        return None


def extract_article(html: str, selector: str | None = None) -> tuple[str, str]:
    """Return (backend_name, text). Tries selector -> trafilatura -> readability
    -> stdlib heuristic; the heuristic always returns something."""
    if selector:
        t = _by_selector(html, selector)
        if t:
            return "selector", t
    for name, fn in (("trafilatura", _try_trafilatura), ("readability", _try_readability)):
        t = fn(html)
        if t:
            return name, t
    return "heuristic", _heuristic_extract(html)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-extract even if the companion is newer")
    ap.add_argument("--selector", default="", help="CSS selector override (needs lxml+cssselect)")
    ap.add_argument("--min-chars", type=int, default=200)
    args = ap.parse_args(argv)

    raw_root = Path(args.root or f"{os.environ.get('WIKI_PATH', '/opt/vault')}/raw")
    if not raw_root.is_dir():
        print(f"ERROR: raw root not found: {raw_root}", file=sys.stderr)
        return 1

    backends = {}
    extracted = skipped = failed = total = 0
    for h in sorted(raw_root.rglob("*")):
        if not h.is_file() or h.suffix.lower() not in (".html", ".htm"):
            continue
        total += 1
        txt = h.with_name(h.name + ".txt")
        if not args.force and txt.is_file() and txt.stat().st_mtime >= h.stat().st_mtime:
            skipped += 1
            continue
        if args.dry_run:
            print(f"DRY: {h} -> {txt}")
            extracted += 1
            continue
        try:
            html = h.read_text(encoding="utf-8", errors="replace")
        except OSError:
            failed += 1
            continue
        backend, text = extract_article(html, args.selector or None)
        if len(text) < args.min_chars:
            print(f"WARN: thin extraction ({len(text)} chars via {backend}, "
                  f"likely boilerplate / JS-rendered): {h}", file=sys.stderr)
            failed += 1
            continue
        txt.write_text(text + "\n", encoding="utf-8")
        backends[backend] = backends.get(backend, 0) + 1
        extracted += 1
        if extracted % 100 == 0:
            print(f"  ... {extracted} extracted, {skipped} skipped, {failed} failed")

    print(f"\nTotal: {total} HTML files scanned")
    print(f"  extracted: {extracted}" + (f"  ({backends})" if backends else ""))
    print(f"  skipped (companion newer): {skipped}")
    print(f"  failed (thin/unreadable): {failed}")
    if backends.get("heuristic") and "trafilatura" not in backends:
        print("  note: used the stdlib heuristic — `pip install trafilatura` for better extraction.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
