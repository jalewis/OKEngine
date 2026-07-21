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
import hashlib
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import web_capture  # noqa: E402
import collection_ledger  # noqa: E402

USER_AGENT = os.environ.get("FEED_USER_AGENT", "okf-engine-feeds/1.0")
TIMEOUT = 20
ATOM_NS = "{http://www.w3.org/2005/Atom}"
MAX_STATE_ENTRIES = 20000
ALLOW_PRIVATE_FEEDS = os.environ.get("FEED_ALLOW_PRIVATE_NETS", "") == "1"
RETRY_STATUS = {429, 500, 502, 503, 504}  # transient — retry with backoff
MAX_RETRIES = 3
MAX_BACKOFF_S = 60
MAX_XML_BYTES = 10 * 1024 * 1024


def _safe_xml_fromstring(blob: bytes | str) -> ET.Element:
    """Parse bounded feed XML without allowing DTD/entity expansion.

    ElementTree does not resolve external entities, but internal entity expansion
    and oversized documents are still unnecessary attack surface for feeds.
    """
    raw = blob.encode("utf-8") if isinstance(blob, str) else blob
    if len(raw) > MAX_XML_BYTES:
        raise ET.ParseError("XML document exceeds 10 MiB safety limit")
    upper = raw[:4096].upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ET.ParseError("DTD/entity declarations are not permitted")
    return ET.fromstring(raw)  # nosec B314


def load_opml_sources(path: Path) -> list[dict]:
    """Configured feeds plus optional provenance classification extensions."""
    try:
        root = _safe_xml_fromstring(path.read_bytes())
    except (OSError, ET.ParseError) as e:
        print(f"ERROR: cannot read OPML {path}: {e}", file=sys.stderr)
        return []
    feeds: list[dict] = []
    for o in root.iter("outline"):
        url = o.get("xmlUrl") or o.get("xmlurl") or ""
        if url:
            label = o.get("text") or o.get("title") or url
            kind = o.get("sourceKind") or o.get("source_kind") or "unknown"
            kind = kind if kind in ("primary", "secondary") else "unknown"
            raw_independent = (o.get("independentOrigin") or
                               o.get("independent_origin") or "").strip().lower()
            independent = (True if raw_independent in ("1", "true", "yes") else
                           False if raw_independent in ("0", "false", "no") else None)
            feeds.append({
                "connector_id": "okengine.feed",
                "source_id": collection_ledger.source_id("okengine.feed", url, label),
                "label": label,
                "url": url,
                "source_kind": kind,
                "independent_origin": independent,
            })
    return feeds


def load_opml(path: Path) -> list[tuple[str, str]]:
    """Compatibility view: [(title, feed_url)]."""
    return [(row["label"], row["url"]) for row in load_opml_sources(path)]


def _error_category(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"http-{exc.code}"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ValueError):
        return "policy-or-config"
    return "network-or-io"


def _publication_latency_ms(items: list[dict], now: datetime) -> int | None:
    values = []
    for item in items:
        try:
            published = datetime.fromisoformat(str(item.get("published") or "").replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            values.append(max(0, int((now - published.astimezone(timezone.utc)).total_seconds() * 1000)))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    values.sort()
    return values[len(values) // 2]


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
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # nosec B310
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


def _children_local(elem: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(elem) if child.tag.rsplit("}", 1)[-1] == name]


def parse_items(blob: bytes, max_per_feed: int) -> list[dict]:
    if blob.lstrip().startswith(b"{"):
        try:
            feed = json.loads(blob)
        except json.JSONDecodeError:
            return []
        if not isinstance(feed, dict) or not str(feed.get("version", "")).startswith(
                "https://jsonfeed.org/version/"):
            return []
        out = []
        for it in (feed.get("items") or [])[:max_per_feed]:
            if not isinstance(it, dict):
                continue
            authors = it.get("authors") or ([it["author"]] if isinstance(it.get("author"), dict) else [])
            out.append({"id": str(it.get("id") or it.get("url") or ""),
                        "title": str(it.get("title") or ""),
                        "link": str(it.get("url") or it.get("external_url") or ""),
                        "summary": str(it.get("summary") or it.get("content_text")
                                       or it.get("content_html") or ""),
                        "published": parse_date(it.get("date_published") or it.get("date_modified")),
                        "authors": [str(a.get("name")) for a in authors
                                    if isinstance(a, dict) and a.get("name")],
                        "tags": [str(x) for x in (it.get("tags") or [])],
                        "language": str(it.get("language") or feed.get("language") or ""),
                        "enclosures": [str(a.get("url")) for a in (it.get("attachments") or [])
                                       if isinstance(a, dict) and a.get("url")]})
        return out
    try:
        root = _safe_xml_fromstring(blob)
    except ET.ParseError:
        return []
    items: list[dict] = []
    channel = root.find("channel")
    if root.tag.endswith("rss") or channel is not None:
        scope = channel if channel is not None else root
        for it in scope.findall("item")[:max_per_feed]:
            link = _t(it.find("link"))
            categories = [_t(x) for x in _children_local(it, "category") if _t(x)]
            enclosures = [x.get("url", "") for x in _children_local(it, "enclosure")
                          if x.get("url")]
            authors = [_t(x) for x in _children_local(it, "author") + _children_local(it, "creator")
                       if _t(x)]
            items.append({
                "id": _t(it.find("guid")) or link,
                "title": _t(it.find("title")),
                "link": link,
                "summary": _t(it.find("description")),
                "published": parse_date(_t(it.find("pubDate"))),
                "authors": authors,
                "tags": categories,
                "language": _t(scope.find("language")),
                "enclosures": enclosures,
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
            authors = [_t(a.find(f"{ATOM_NS}name")) for a in it.findall(f"{ATOM_NS}author")]
            items.append({
                "id": _t(it.find(f"{ATOM_NS}id")) or link,
                "title": _t(it.find(f"{ATOM_NS}title")),
                "link": link,
                "summary": _t(summ),
                "published": parse_date(pub),
                "authors": [a for a in authors if a],
                "tags": [c.get("term", "") for c in it.findall(f"{ATOM_NS}category")
                         if c.get("term")],
                "language": it.get("{http://www.w3.org/XML/1998/namespace}lang", "")
                or root.get("{http://www.w3.org/XML/1998/namespace}lang", ""),
                "enclosures": [x.get("href", "") for x in it.findall(f"{ATOM_NS}link[@rel='enclosure']")
                               if x.get("href")],
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
    if len(state.get("captures", {})) > MAX_STATE_ENTRIES:
        retained = set(state.get("seen", {}))
        candidates = [(key, value) for key, value in state["captures"].items()
                      if key in retained]
        candidates.sort(key=lambda item: str(item[1].get("retrieved_at", "")), reverse=True)
        state["captures"] = dict(candidates[:MAX_STATE_ENTRIES])
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
    revision = str(it.get("_revision_hash") or "")[:8]
    suffix = f"-revision-{revision}" if revision else ""
    name = f"{date}-{slugify(it.get('title'))}{suffix}.md"
    dst = out_dir / name
    if dst.exists():
        identity = hashlib.sha256(str(it.get("id") or it.get("link") or "").encode()).hexdigest()[:8]
        dst = out_dir / f"{date}-{slugify(it.get('title'))}{suffix}-{identity}.md"
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
        f"canonical_url: {json.dumps(it.get('canonical_url'))}" if it.get("canonical_url") else "",
        f"retrieved_url: {json.dumps(it.get('retrieved_url'))}" if it.get("retrieved_url") else "",
        f"source_native_id: {json.dumps(it.get('id'))}" if it.get("id") else "",
        f"authors: {json.dumps(it.get('authors'))}" if it.get("authors") else "",
        f"source_tags: {json.dumps(it.get('tags'))}" if it.get("tags") else "",
        f"language: {json.dumps(it.get('language'))}" if it.get("language") else "",
        f"license: {json.dumps(it.get('license'))}" if it.get("license") else "",
        f"enclosures: {json.dumps(it.get('enclosures'))}" if it.get("enclosures") else "",
        f"content_hash: {it.get('content_hash')}" if it.get("content_hash") else "",
        f"capture_object: {json.dumps(it.get('capture_object'))}" if it.get("capture_object") else "",
        f"capture_revision: {json.dumps(it.get('capture_revision'))}" if it.get("capture_revision") else "",
        f"revision_of: {json.dumps(it.get('id'))}" if revision else "",
        f"source_correction: true" if it.get("source_correction") else "",
        f"source_retraction: true" if it.get("source_retraction") else "",
        f"capture_dead_letter: {json.dumps(it.get('capture_dead_letter'))}"
        if it.get("capture_dead_letter") else "",
        f"published: {pub}" if pub else "published:",
        f"fetched: {datetime.now(timezone.utc).isoformat()}",
        f"watch_lane: {tag}" if tag else "",
        "---",
        "",
        f"# {it.get('title') or '(untitled)'}",
        "",
        "## Captured content" if it.get("captured_text") else "",
        "",
        it.get("captured_text") or summary,
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
    ap.add_argument("--capture-full-text", action="store_true",
                    help="opt in to bounded linked-document capture and revision detection")
    ap.add_argument("--capture-dir", default="",
                    help="content-addressed capture store (default: OUT_DIR sibling captures/)")
    ap.add_argument("--collection-ledger", default=os.environ.get("COLLECTION_LEDGER_DIR", ""),
                    help="collection telemetry directory (deployed default: /opt/data/collection)")
    args = ap.parse_args(argv)

    opml = Path(args.opml)
    configured = load_opml_sources(opml)
    feeds = [(row["label"], row["url"]) for row in configured]
    ledger_dir = (Path(args.collection_ledger) if args.collection_ledger else
                  (Path("/opt/data/collection") if Path("/opt/data").is_dir() else None))
    ledger_errors: list[str] = []
    if ledger_dir is not None:
        try:
            collection_ledger.register_sources(
                ledger_dir, configured, connector_id="okengine.feed")
        except (OSError, ValueError) as exc:
            ledger_errors.append(f"source registry: {exc}")
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
    capture_dir = (Path(args.capture_dir) if args.capture_dir else
                   ((out_dir.parent / "captures") if out_dir else None))
    if args.capture_full_text and capture_dir is None:
        ap.error("--capture-full-text requires --out-dir or --capture-dir")
    captures = state.setdefault("captures", {})

    new_by_feed: dict[str, list[dict]] = {}
    written = 0
    unchanged = 0
    capture_errors = 0
    errors: list[str] = []
    configured_by_url = {row["url"]: row for row in configured}
    for name, url in feeds:
        source = configured_by_url[url]
        started = datetime.now(timezone.utc)
        tick = time.monotonic()
        checkpoint_in = collection_ledger.checkpoint_digest(http_state.get(url))

        def record(outcome: str, *, fetched=0, extracted=0, accepted=0, rejected=0,
                   deduped=0, dead_letter=0, error_category=None, items=None):
            if ledger_dir is None:
                return
            finished = datetime.now(timezone.utc)
            try:
                collection_ledger.append_attempt(ledger_dir, {
                    "connector_id": source["connector_id"], "source_id": source["source_id"],
                    "started_at": started, "finished_at": finished, "outcome": outcome,
                    "fetched": fetched, "extracted": extracted, "accepted": accepted,
                    "rejected": rejected, "deduped": deduped, "dead_letter": dead_letter,
                    "latency_ms": int((time.monotonic() - tick) * 1000),
                    "error_category": error_category, "checkpoint_in": checkpoint_in,
                    "checkpoint_out": collection_ledger.checkpoint_digest(http_state.get(url)),
                    "newest_published_at": max((str(x.get("published") or "") for x in (items or [])),
                                               default="") or None,
                    "publication_to_ingest_ms": _publication_latency_ms(items or [], finished),
                })
            except (OSError, ValueError) as exc:
                ledger_errors.append(f"{name}: {exc}")
        try:
            body, validators = fetch(url, http_state.get(url))
        except NotModified:
            unchanged += 1
            record("success")
            continue
        except (ValueError, urllib.error.URLError, TimeoutError, OSError) as e:
            errors.append(f"{name}: fetch failed ({e})")
            record("failure", error_category=_error_category(e))
            continue
        http_state[url] = validators
        items = parse_items(body, args.max_per_feed)
        if not items:
            errors.append(f"{name}: 0 items parsed")
            record("failure", error_category="parse-empty")
            continue
        novel = []
        feed_capture_errors = 0
        for it in items:
            native_id = it.get("id") or it.get("link") or ""
            seen_key = f"{url}\0{native_id}"
            # The legacy state used bare native IDs. Honor it for upgrade
            # compatibility, but write the feed-scoped key so two publishers
            # reusing a GUID cannot suppress each other going forward.
            first_seen = bool(native_id and seen_key not in seen and native_id not in seen)
            revision = False
            if args.capture_full_text and it.get("link"):
                previous = captures.get(seen_key) or {}
                was_removed = bool(previous.get("upstream_removed"))
                feed_fingerprint = hashlib.sha256(json.dumps(
                    {key: it.get(key) for key in ("title", "summary", "published", "authors",
                                                  "tags", "language", "enclosures")},
                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                feed_changed = bool(previous and previous.get("feed_fingerprint") != feed_fingerprint)
                try:
                    captured = web_capture.capture(
                        capture_dir, it["link"], native_id=native_id, publisher=name,
                        previous=previous, observed_at=now)
                    captured.state["feed_fingerprint"] = feed_fingerprint
                    captures[seen_key] = captured.state
                    it.update({"canonical_url": captured.canonical_url,
                               "retrieved_url": captured.final_url,
                               "content_hash": captured.content_hash,
                               "capture_object": captured.object_ref,
                               "capture_revision": captured.revision_ref,
                               "captured_text": captured.text})
                    if captured.author:
                        it["authors"] = list(dict.fromkeys(
                            [*(it.get("authors") or []), captured.author]))
                    if captured.tags:
                        it["tags"] = list(dict.fromkeys(
                            [*(it.get("tags") or []), *captured.tags]))
                    if captured.language:
                        it["language"] = captured.language
                    if captured.license:
                        it["license"] = captured.license
                    revision = bool(previous and (captured.changed or feed_changed or was_removed))
                    if revision:
                        it["_revision_hash"] = hashlib.sha256(
                            f"{captured.content_hash}\0{feed_fingerprint}".encode()).hexdigest()
                except web_capture.CaptureError as exc:
                    capture_errors += 1
                    feed_capture_errors += 1
                    it["capture_dead_letter"] = web_capture.dead_letter(
                        capture_dir, it["link"], native_id, exc, observed_at=now)
                    if exc.category == "upstream-removed":
                        it["source_retraction"] = True
                        revision = bool(previous and not was_removed)
                        if previous:
                            previous["upstream_removed"] = True
                            previous["retrieved_at"] = now
                            captures[seen_key] = previous
                        if revision:
                            it["_revision_hash"] = hashlib.sha256(
                                f"{previous.get('content_hash', '')}\0{exc.category}".encode()
                            ).hexdigest()
            label = f"{it.get('title', '')} {it.get('summary', '')}".casefold()
            it["source_correction"] = "correction" in label or "corrected" in label
            it["source_retraction"] = bool(it.get("source_retraction")
                                            or "retraction" in label or "retracted" in label)
            if native_id and (first_seen or revision):
                it["_seen_key"] = seen_key
                novel.append(it)
        for it in novel:
            seen[it.get("_seen_key") or it["id"]] = now
            if out_dir is not None and write_item(out_dir, name, it, args.source_tag):
                written += 1
        if novel:
            new_by_feed[name] = novel
        record("partial" if feed_capture_errors else "success",
               fetched=len(items), extracted=len(items), accepted=len(novel),
               rejected=sum(not (it.get("id") or it.get("link")) for it in items),
               deduped=max(0, len(items) - len(novel)), dead_letter=feed_capture_errors,
               error_category="capture-dead-letter" if feed_capture_errors else None,
               items=novel)

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
              f"{len(errors)} feed errors; {capture_errors} capture dead letters")
        for e in errors[:20]:
            print(f"  ! {e}")
        for e in ledger_errors[:20]:
            print(f"  ! collection telemetry unavailable ({e})")
    # script-only by default (no_agent pull); --digest path is agent-consumed
    if not args.digest:
        print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
