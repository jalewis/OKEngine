#!/usr/bin/env python3
"""Vault-wide render lint — crawl EVERY page of a live deployment through the reader's actual render
path and assert the rendered OUTPUT is clean.

The unit suite and the smoke harness assert on IDEALIZED fixtures; the bugs that reach users live in
REAL data — a page whose rendered HTML leaks builder markup, an unrendered wikilink showing as
literal `[[…]]`, a broken embed. Every recent user-visible render bug (HTML-in-the-UI, backtick
wikilinks, source-link leaks) was a render defect on stored content that a source/schema audit and
a clean-fixture test both pass. This sweeps the whole vault through the reader and flags the output.

It hits the reader's HTTP API (`/api/pages` to enumerate, `/api/page` to render), so it tests the
EXACT bytes a user sees — no local re-render that could diverge from production.

Usage (on-demand):
    python scripts/render_lint.py --reader-url http://127.0.0.1:9400
    python scripts/render_lint.py --reader-url ... --limit 500        # quick sample
    python scripts/render_lint.py --reader-url ... --write-vault /opt/vault   # write the dashboard
Exit code: 0 = clean (within --max-offenders), 1 = offenders over the threshold, 2 = usage/reachability.

As a cron: point --reader-url at the in-network reader service and pass --write-vault; it writes
wiki/operational/render-lint.md and exits non-zero when the fleet regresses.
"""
import argparse
import datetime
import concurrent.futures
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ── the checks (pure; unit-tested) ───────────────────────────────────────────

_CODE_RE = re.compile(r"<(pre|code)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _visible_prose(html: str) -> str:
    """Rendered text with tags removed AND <code>/<pre> spans dropped — a literal `[[x]]` inside a
    code span is intentional (documenting a wikilink), so only PROSE residue is a real leak."""
    return _TAG_RE.sub(" ", _CODE_RE.sub(" ", html))


def lint_html(path: str, html: str) -> list[str]:
    """Return the violation codes for one rendered page (empty = clean).

    - wl-markup-leak : the builder's `<a class="wl">` anchor got HTML-escaped and shows as literal
                       text (the HTML-in-the-UI bug) — `&lt;a class="wl"` in the output.
    - literal-wikilink: a `[[…]]` survived unrendered in visible PROSE (a wikilink the renderer
                       failed to turn into a link or plain text).
    - backtick-wikilink: backtick residue around a wikilink in prose (the _uncode_wikilinks case
                       leaving `` `[[ `` / `]]` `` behind).
    - unresolved-embed: an `![[…]]` transclusion left unrendered in visible prose.
    """
    v: list[str] = []
    low = html.lower()
    if "&lt;a class=\"wl\"" in low or "&lt;a class=&quot;wl&quot;" in low:
        v.append("wl-markup-leak")
    prose = _visible_prose(html)
    if "![[" in prose:
        v.append("unresolved-embed")
    if "`[[" in prose or "]]`" in prose:
        v.append("backtick-wikilink")
    # a bare [[…]] left in prose (exclude the embed/backtick cases already counted)
    if re.search(r"(?<!!)\[\[[^\]]+\]\]", prose):
        v.append("literal-wikilink")
    return v


# ── the crawl ────────────────────────────────────────────────────────────────

def _get_json(url: str, timeout: float = 30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _get_text(url: str, timeout: float = 30) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def enumerate_pages(reader_url: str) -> list[str]:
    d = _get_json(f"{reader_url}/api/pages")
    pages = d.get("pages", d) if isinstance(d, dict) else d
    return [p["path"] for p in pages if isinstance(p, dict) and p.get("path")]


def _lint_one(reader_url: str, path: str, retries: int = 2) -> tuple[str, list[str]]:
    # Retry a failed fetch before recording it: a single-worker reader under a concurrent sweep will
    # occasionally time out a request, which is a crawler artifact, NOT a page defect. Only a page
    # that fails EVERY attempt is a real fetch-error (a genuinely un-renderable page).
    url = f"{reader_url}/api/page?path={urllib.parse.quote(path)}"
    for attempt in range(retries + 1):
        try:
            d = _get_json(url, timeout=60)
            return path, lint_html(path, d.get("html", "") or "")
        except Exception:
            if attempt == retries:
                return path, ["fetch-error"]
    return path, ["fetch-error"]


def crawl(reader_url: str, paths: list[str], workers: int = 16) -> dict[str, list[str]]:
    offenders: dict[str, list[str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for path, viol in ex.map(lambda p: _lint_one(reader_url, p), paths):
            if viol:
                offenders[path] = viol
    return offenders


# ── report ───────────────────────────────────────────────────────────────────

def render_report(total: int, offenders: dict[str, list[str]], now: str) -> str:
    by_code: dict[str, int] = {}
    for viol in offenders.values():
        for c in viol:
            by_code[c] = by_code.get(c, 0) + 1
    n = len(offenders)
    L = ["---", "type: dashboard", 'title: "Render lint"', f"updated: {now}", "---", "",
         f"# Render lint — {now}", "",
         f"Swept **{total:,}** pages through the reader's render path. "
         f"**{n:,}** page(s) with a rendered-output defect.", ""]
    if by_code:
        L += ["| Violation | Pages |", "|---|---|"]
        L += [f"| {c} | {k} |" for c, k in sorted(by_code.items(), key=lambda x: -x[1])]
        L += ["", "## Offenders", "", "| Page | Violations |", "|---|---|"]
        for p in sorted(offenders)[:500]:
            L.append(f"| {p} | {', '.join(offenders[p])} |")
        if n > 500:
            L.append(f"| … | +{n - 500:,} more |")
    else:
        L.append("_No rendered-output defects. Clean._")
    L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Vault-wide render lint over the reader")
    # CRON MODE: invoked arg-less by cron-plus (no_agent). The reader defaults to the standard
    # in-network service (override with OKENGINE_READER_URL), and WIKI_PATH in the gateway env makes
    # it WRITE the dashboard automatically — like the other no_agent audit lanes. On-demand
    # (`make render-lint`, explicit --reader-url) prints only unless --write-vault.
    ap.add_argument("--reader-url", default=os.environ.get("OKENGINE_READER_URL")
                    or ("http://okengine-reader:9200" if os.environ.get("WIKI_PATH") else "http://127.0.0.1:9400"))
    ap.add_argument("--limit", type=int, default=0, help="cap pages crawled (0 = all)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--max-offenders", type=int, default=0, help="offenders tolerated before exit 1")
    ap.add_argument("--write-vault", default="", help="vault root; writes wiki/operational/render-lint.md")
    ap.add_argument("--now", default="", help="timestamp for the report (deployment stamps it)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.write_vault and os.environ.get("WIKI_PATH"):      # cron mode -> auto-write the dashboard
        wp = Path(os.environ["WIKI_PATH"])
        a.write_vault = str(wp.parent if wp.name == "wiki" else wp)

    try:
        paths = enumerate_pages(a.reader_url)
    except (urllib.error.URLError, OSError) as e:
        print(f"render-lint: reader unreachable at {a.reader_url} ({e})", file=sys.stderr)
        return 2
    if a.limit:
        paths = paths[:a.limit]
    offenders = crawl(a.reader_url, paths, workers=a.workers)

    if a.write_vault:
        out = Path(a.write_vault) / "wiki" / "operational" / "render-lint.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_report(len(paths), offenders, a.now or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")), encoding="utf-8")
        print(f"render-lint: wrote {out}")

    if a.json:
        print(json.dumps({"total": len(paths), "offenders": offenders}, indent=2))
    else:
        by_code: dict[str, int] = {}
        for viol in offenders.values():
            for c in viol:
                by_code[c] = by_code.get(c, 0) + 1
        print(f"render-lint: swept {len(paths):,} pages, {len(offenders):,} with defects "
              f"{dict(sorted(by_code.items(), key=lambda x: -x[1]))}")
        for p in sorted(offenders)[:20]:
            print(f"  {p}: {', '.join(offenders[p])}")
        if len(offenders) > 20:
            print(f"  … +{len(offenders) - 20:,} more")
    return 1 if len(offenders) > a.max_offenders else 0


if __name__ == "__main__":
    raise SystemExit(main())
