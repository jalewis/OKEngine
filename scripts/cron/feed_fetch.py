#!/usr/bin/env python3
"""Generic, config-driven RSS/Atom feed fetcher (ENGINE).

Reads a feed list from an OPML file (feeds = pure config), fetches/parses each,
dedupes against a state file, and either:
  - writes each new item as a markdown file into an output dir (raw landing), and/or
  - emits a markdown digest to stdout (for an agent-consumption cron).

This is the reusable feed mechanism — a new domain supplies an OPML, no code
change (replaces the per-domain hardcoded FEEDS lists). RSS + Atom supported.

Usage:
  feed_fetch.py --opml FEEDS.opml [--out-dir DIR] [--digest] \
                [--state STATE.json] [--source-tag TAG] [--max-per-feed N]

  # example raw landing (no_agent pull):
  feed_fetch.py --opml /opt/data/config/feeds.opml \
                --out-dir /opt/vault/raw/feeds \
                --state /opt/data/scripts/feed-state.json --source-tag feeds
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import ipaddress
import socket
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

USER_AGENT = os.environ.get("FEED_USER_AGENT", "okf-engine-feeds/1.0")
TIMEOUT = 20
ATOM_NS = "{http://www.w3.org/2005/Atom}"
MAX_STATE_ENTRIES = 20000
ALLOW_PRIVATE_FEEDS = os.environ.get("FEED_ALLOW_PRIVATE_NETS", "") == "1"
RETRY_STATUS = {429, 500, 502, 503, 504}  # transient — retry with backoff
MAX_RETRIES = 3
MAX_BACKOFF_S = 60


def load_opml(path: Path) -> list[tuple[str, str]]:
    """Return [(title, feed_url)] from every <outline> carrying a feed URL."""
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError) as e:
        print(f"ERROR: cannot read OPML {path}: {e}", file=sys.stderr)
        return []
    feeds: list[tuple[str, str]] = []
    for o in root.iter("outline"):
        url = o.get("xmlUrl") or o.get("xmlurl") or ""
        if url:
            feeds.append((o.get("text") or o.get("title") or url, url))
    return feeds


class NotModified(Exception):
    """A conditional request returned 304 Not Modified (feed unchanged)."""


def _retry_after(err: urllib.error.HTTPError, attempt: int) -> float:
    """Seconds to wait before a retry: honor Retry-After (delta or HTTP-date),
    else capped exponential backoff."""
    ra = err.headers.get("Retry-After") if err.headers else None
    if ra:
        try:
            return float(min(MAX_BACKOFF_S, max(0, int(ra))))
        except ValueError:
            try:
                when = parsedate_to_datetime(ra)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                delay = (when - datetime.now(timezone.utc)).total_seconds()
                return min(float(MAX_BACKOFF_S), max(0.0, delay))
            except (TypeError, ValueError):
                pass
    return float(min(MAX_BACKOFF_S, 2 ** attempt))


def fetch(url: str, validators: dict | None = None) -> tuple[bytes, dict]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("unsupported feed URL scheme/host")
    if not ALLOW_PRIVATE_FEEDS:
        try:
            infos = socket.getaddrinfo(parsed.hostname, parsed.port, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise ValueError(f"cannot resolve feed host: {e}") from e
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
                raise ValueError(f"refusing private/link-local feed host address: {ip}")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/atom+xml, application/rss+xml, application/xml;q=0.9, */*;q=0.8",
    }
    validators = validators or {}
    if validators.get("etag"):
        headers["If-None-Match"] = validators["etag"]
    if validators.get("last_modified"):
        headers["If-Modified-Since"] = validators["last_modified"]
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = resp.read()
                fresh: dict[str, str] = {}
                if resp.headers.get("ETag"):
                    fresh["etag"] = resp.headers["ETag"]
                if resp.headers.get("Last-Modified"):
                    fresh["last_modified"] = resp.headers["Last-Modified"]
                return body, fresh
        except urllib.error.HTTPError as e:
            if e.code == 304:
                raise NotModified from None
            if e.code in RETRY_STATUS and attempt < MAX_RETRIES - 1:
                time.sleep(_retry_after(e, attempt))
                continue
            raise
    raise RuntimeError("unreachable: retry loop must return or raise")


def parse_date(s: str | None) -> str:
    if not s:
        return ""
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return s.strip()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _t(elem: ET.Element | None) -> str:
    return elem.text.strip() if elem is not None and elem.text else ""


def parse_items(blob: bytes, max_per_feed: int) -> list[dict]:
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        return []
    items: list[dict] = []
    channel = root.find("channel")
    if root.tag.endswith("rss") or channel is not None:
        scope = channel if channel is not None else root
        for it in scope.findall("item")[:max_per_feed]:
            link = _t(it.find("link"))
            items.append({
                "id": _t(it.find("guid")) or link,
                "title": _t(it.find("title")),
                "link": link,
                "summary": _t(it.find("description")),
                "published": parse_date(_t(it.find("pubDate"))),
            })
    elif root.tag == f"{ATOM_NS}feed":
        for it in root.findall(f"{ATOM_NS}entry")[:max_per_feed]:
            le = it.find(f"{ATOM_NS}link[@rel='alternate']")
            if le is None:
                le = it.find(f"{ATOM_NS}link")
            link = le.get("href", "") if le is not None else ""
            summ = it.find(f"{ATOM_NS}summary")
            if summ is None:
                summ = it.find(f"{ATOM_NS}content")
            pub = _t(it.find(f"{ATOM_NS}published")) or _t(it.find(f"{ATOM_NS}updated"))
            items.append({
                "id": _t(it.find(f"{ATOM_NS}id")) or link,
                "title": _t(it.find(f"{ATOM_NS}title")),
                "link": link,
                "summary": _t(summ),
                "published": parse_date(pub),
            })
    return items


def load_state(p: Path) -> dict:
    if not p.exists():
        return {"seen": {}}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {"seen": {}}


def save_state(p: Path, state: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    if len(state.get("seen", {})) > MAX_STATE_ENTRIES:
        ordered = sorted(state["seen"].items(), key=lambda kv: kv[1], reverse=True)
        state["seen"] = dict(ordered[:MAX_STATE_ENTRIES])
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, p)


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "untitled").lower()).strip("-")
    return (s or "untitled")[:80]


def write_item(out_dir: Path, feed: str, it: dict, tag: str) -> Path | None:
    pub = it.get("published") or ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", pub)
    date = m.group(1) if m else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name = f"{date}-{slugify(it.get('title'))}.md"
    dst = out_dir / name
    if dst.exists():
        return None
    summary = " ".join((it.get("summary") or "").split())
    fm = [
        "---",
        "type: source",
        "source_channel: feed",
        f"source_feed: {json.dumps(feed)}",
        f"title: {json.dumps(it.get('title') or '(untitled)')}",
        f"url: {json.dumps(it.get('link') or '')}",
        f"published: {pub}" if pub else "published:",
        f"fetched: {datetime.now(timezone.utc).isoformat()}",
        f"watch_lane: {tag}" if tag else "",
        "---",
        "",
        f"# {it.get('title') or '(untitled)'}",
        "",
        summary,
        "",
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(l for l in fm if l is not None), encoding="utf-8")
    return dst


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--opml", required=True)
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--digest", action="store_true", help="emit markdown digest to stdout")
    ap.add_argument("--state", default="")
    ap.add_argument("--source-tag", default="")
    ap.add_argument("--max-per-feed", type=int, default=25)
    args = ap.parse_args(argv)

    opml = Path(args.opml)
    feeds = load_opml(opml)
    if not feeds:
        # An empty/absent feed list is the safe out-of-the-box default: the
        # pack ships with feed-fetch ENABLED but feeds.opml EMPTY, so a fresh
        # install runs the loop yet makes zero upstream calls until the operator
        # populates feeds.opml. That's a clean no-op, NOT an error — returning
        # nonzero here would make the scheduled cron log a failure every run.
        print(f"no feeds configured in {opml} — nothing to fetch "
              f"(populate it to start ingesting)")
        return 0
    state_path = Path(args.state or (opml.with_suffix(".state.json")))
    state = load_state(state_path)
    seen = state.setdefault("seen", {})
    http_state = state.setdefault("http", {})  # per-feed ETag / Last-Modified
    now = datetime.now(timezone.utc).isoformat()
    out_dir = Path(args.out_dir) if args.out_dir else None

    new_by_feed: dict[str, list[dict]] = {}
    written = 0
    unchanged = 0
    errors: list[str] = []
    for name, url in feeds:
        try:
            body, validators = fetch(url, http_state.get(url))
        except NotModified:
            unchanged += 1
            continue
        except (ValueError, urllib.error.URLError, TimeoutError, OSError) as e:
            errors.append(f"{name}: fetch failed ({e})")
            continue
        http_state[url] = validators
        items = parse_items(body, args.max_per_feed)
        if not items:
            errors.append(f"{name}: 0 items parsed")
            continue
        novel = [it for it in items if it["id"] and it["id"] not in seen]
        for it in novel:
            seen[it["id"]] = now
            if out_dir is not None and write_item(out_dir, name, it, args.source_tag):
                written += 1
        if novel:
            new_by_feed[name] = novel

    state["last_run"] = now
    feed_urls = {u for _, u in feeds}
    state["http"] = {u: v for u, v in http_state.items() if u in feed_urls}
    save_state(state_path, state)

    total = sum(len(v) for v in new_by_feed.values())
    if args.digest:
        print(f"# Feed digest — {now}\n")
        print(f"**{total} new items across {len(new_by_feed)}/{len(feeds)} feeds.**\n")
        for nm, its in new_by_feed.items():
            print(f"## {nm} ({len(its)})\n")
            for it in its:
                print(f"### {it['title'] or '(untitled)'}")
                if it["published"]:
                    print(f"*Published: {it['published']}*")
                if it["link"]:
                    print(f"<{it['link']}>")
                s = " ".join((it["summary"] or "").split())[:400]
                if s:
                    print(f"\n{s}\n")
                print()
    else:
        print(f"feed_fetch: {total} new items across {len(new_by_feed)}/{len(feeds)} feeds; "
              f"{written} written to {out_dir}; {unchanged} unchanged (304); "
              f"{len(errors)} feed errors")
        for e in errors[:20]:
            print(f"  ! {e}")
    # script-only by default (no_agent pull); --digest path is agent-consumed
    if not args.digest:
        print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
