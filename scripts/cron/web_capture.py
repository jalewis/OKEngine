#!/usr/bin/env python3
"""Bounded, revision-aware capture of linked web documents.

The capture store is content addressed and append-only. Raw response bodies are
written once under ``objects/``; provenance observations are written once under
``revisions/``. Re-fetching unchanged content creates no new artifact. Failures
become deterministic dead-letter records instead of disappearing into stderr.

This module deliberately supports HTML and plain text only. PDF, Office, OCR,
and browser-rendered capture belong to the hard-format extraction capability.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

USER_AGENT = os.environ.get("CAPTURE_USER_AGENT", "okf-engine-capture/1.0")
TIMEOUT = int(os.environ.get("CAPTURE_TIMEOUT", "20"))
MAX_BYTES = int(os.environ.get("CAPTURE_MAX_BYTES", str(5 * 1024 * 1024)))
MAX_RETRIES = 3
MAX_BACKOFF_S = 60
RETRY_STATUS = {429, 500, 502, 503, 504}
ALLOWED_CONTENT_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
ALLOW_PRIVATE = os.environ.get("CAPTURE_ALLOW_PRIVATE_NETS", "") == "1"


class CaptureError(Exception):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


class CaptureNotModified(Exception):
    pass


@dataclass(frozen=True)
class CaptureResult:
    requested_url: str
    final_url: str
    canonical_url: str
    content_hash: str
    content_type: str
    retrieved_at: str
    object_ref: str
    revision_ref: str
    text: str
    title: str
    author: str
    language: str
    license: str
    tags: list[str]
    changed: bool
    state: dict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise CaptureError("invalid-url", "unsupported capture URL scheme/host")
    if parsed.username is not None or parsed.password is not None:
        raise CaptureError("invalid-url", "capture URLs must not contain credentials")
    if ALLOW_PRIVATE:
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise CaptureError("dns", f"cannot resolve capture host: {exc}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_reserved):
            raise CaptureError("ssrf", f"refusing non-public capture host address: {address}")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_open(req, timeout):
    return urllib.request.build_opener(_SafeRedirect()).open(req, timeout=timeout)


def _header(headers, name: str) -> str:
    return str(headers.get(name) or "").strip()


def _retry_after(error: urllib.error.HTTPError, attempt: int) -> float:
    value = _header(error.headers or {}, "Retry-After")
    if value.isdigit():
        return float(min(MAX_BACKOFF_S, int(value)))
    return float(min(MAX_BACKOFF_S, 2 ** attempt))


def fetch_document(url: str, previous: dict | None = None, *, opener=None) -> tuple[bytes, dict]:
    """Fetch one bounded document and return body plus response metadata."""
    _validate_url(url)
    previous = previous or {}
    headers = {"User-Agent": USER_AGENT,
               "Accept": "text/html, application/xhtml+xml, text/plain;q=0.9"}
    if previous.get("etag"):
        headers["If-None-Match"] = previous["etag"]
    if previous.get("last_modified"):
        headers["If-Modified-Since"] = previous["last_modified"]
    request = urllib.request.Request(url, headers=headers)
    open_request = opener or _default_open
    for attempt in range(MAX_RETRIES):
        try:
            with open_request(request, TIMEOUT) as response:
                final_url = response.geturl() if hasattr(response, "geturl") else url
                _validate_url(final_url)
                content_type = _header(response.headers, "Content-Type").split(";", 1)[0].lower()
                if content_type not in ALLOWED_CONTENT_TYPES:
                    raise CaptureError("unsupported-content", content_type or "missing Content-Type")
                length = _header(response.headers, "Content-Length")
                if length.isdigit() and int(length) > MAX_BYTES:
                    raise CaptureError("oversize", f"Content-Length {length} exceeds {MAX_BYTES}")
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise CaptureError("oversize", f"response exceeds {MAX_BYTES} bytes")
                return body, {
                    "final_url": final_url,
                    "content_type": content_type,
                    "etag": _header(response.headers, "ETag"),
                    "last_modified": _header(response.headers, "Last-Modified"),
                    "status": int(getattr(response, "status", 200)),
                }
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                raise CaptureNotModified from None
            if exc.code in {404, 410}:
                raise CaptureError("upstream-removed", f"HTTP {exc.code}") from exc
            if exc.code in RETRY_STATUS and attempt < MAX_RETRIES - 1:
                time.sleep(_retry_after(exc, attempt))
                continue
            raise CaptureError("http", f"HTTP {exc.code}") from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            if attempt < MAX_RETRIES - 1:
                time.sleep(min(MAX_BACKOFF_S, 2 ** attempt))
                continue
            raise CaptureError("network", str(exc)) from exc
    raise CaptureError("network", "retry budget exhausted")


class _HTMLText(HTMLParser):
    BLOCKS = {"article", "blockquote", "br", "div", "h1", "h2", "h3", "h4", "li", "p", "pre"}
    SKIP = {"script", "style", "svg", "template", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.title_parts: list[str] = []
        self.in_title = False
        self.canonical = ""
        self.author = ""
        self.language = ""
        self.license = ""
        self.tags: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = {str(k).lower(): str(v or "") for k, v in attrs}
        tag = tag.lower()
        if tag == "html":
            self.language = attrs.get("lang", "")
        if tag in self.SKIP:
            self.skip_depth += 1
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            key = (attrs.get("name") or attrs.get("property") or "").lower()
            value = attrs.get("content", "").strip()
            if key in {"author", "article:author"} and not self.author:
                self.author = value
            elif key in {"keywords", "article:tag"}:
                self.tags.extend(x.strip() for x in value.split(",") if x.strip())
            elif key in {"content-language", "og:locale"} and not self.language:
                self.language = value
        if tag == "link":
            rel = {x.casefold() for x in attrs.get("rel", "").split()}
            if "canonical" in rel:
                self.canonical = attrs.get("href", "")
            if "license" in rel:
                self.license = attrs.get("href", "")
        if tag in self.BLOCKS and self.parts and self.parts[-1] != "\n":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1
        if tag == "title":
            self.in_title = False
        if tag in self.BLOCKS and self.parts and self.parts[-1] != "\n":
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip_depth:
            return
        value = " ".join(data.split())
        if not value:
            return
        if self.in_title:
            self.title_parts.append(value)
        self.parts.append(value + " ")


def extract(body: bytes, content_type: str, final_url: str) -> dict:
    text = body.decode("utf-8", errors="replace")
    if content_type == "text/plain":
        return {"text": text.strip(), "title": "", "canonical_url": final_url,
                "author": "", "language": "", "license": "", "tags": []}
    parser = _HTMLText()
    try:
        parser.feed(text)
    except Exception as exc:
        raise CaptureError("extraction", str(exc)) from exc
    rendered = "\n".join(
        line.strip() for line in "".join(parser.parts).splitlines() if line.strip())
    if not rendered:
        raise CaptureError("extraction", "HTML yielded no readable text")
    canonical = urljoin(final_url, parser.canonical) if parser.canonical else final_url
    _validate_url(canonical)
    return {"text": rendered, "title": " ".join(parser.title_parts).strip(),
            "canonical_url": canonical, "author": parser.author,
            "language": parser.language, "license": parser.license,
            "tags": list(dict.fromkeys(parser.tags))}


def _write_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
    except FileExistsError:
        pass


def dead_letter(root: Path, url: str, native_id: str, error: CaptureError,
                *, observed_at: str | None = None) -> str:
    key = hashlib.sha256(f"{url}\0{native_id}\0{error.category}\0{error}".encode()).hexdigest()
    rel = Path("dead-letter") / key[:2] / f"{key}.json"
    payload = {"requested_url": url, "source_native_id": native_id,
               "category": error.category, "error": str(error),
               "observed_at": observed_at or _now()}
    _write_once(root / rel, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode())
    return rel.as_posix()


def capture(root: Path, url: str, *, native_id: str = "", publisher: str = "",
            previous: dict | None = None, opener=None, observed_at: str | None = None) -> CaptureResult:
    previous = previous or {}
    observed_at = observed_at or _now()
    try:
        body, response = fetch_document(url, previous, opener=opener)
    except CaptureNotModified:
        return CaptureResult(
            requested_url=url, final_url=previous.get("final_url", url),
            canonical_url=previous.get("canonical_url", previous.get("final_url", url)),
            content_hash=previous.get("content_hash", ""), content_type=previous.get("content_type", ""),
            retrieved_at=observed_at, object_ref=previous.get("object_ref", ""),
            revision_ref=previous.get("revision_ref", ""), text="", title="", author="",
            language="", license="", tags=[], changed=False, state=previous)
    content_hash = hashlib.sha256(body).hexdigest()
    fields = extract(body, response["content_type"], response["final_url"])
    suffix = ".html" if response["content_type"] != "text/plain" else ".txt"
    object_rel = Path("objects") / content_hash[:2] / f"{content_hash}{suffix}"
    _write_once(root / object_rel, body)
    url_key = hashlib.sha256(fields["canonical_url"].encode()).hexdigest()
    observation_key = hashlib.sha256(
        f"{native_id}\0{publisher}\0{content_hash}".encode()).hexdigest()[:16]
    revision_rel = Path("revisions") / url_key[:2] / url_key / f"{content_hash}-{observation_key}.json"
    state = {"requested_url": url, "final_url": response["final_url"],
             "canonical_url": fields["canonical_url"], "content_hash": content_hash,
             "content_type": response["content_type"], "etag": response.get("etag", ""),
             "last_modified": response.get("last_modified", ""),
             "object_ref": object_rel.as_posix(), "revision_ref": revision_rel.as_posix(),
             "retrieved_at": observed_at}
    record = {**state, "source_native_id": native_id, "publisher": publisher,
              "response_status": response["status"], "title": fields["title"],
              "author": fields["author"], "language": fields["language"],
              "license": fields["license"], "tags": fields["tags"]}
    _write_once(root / revision_rel, (json.dumps(record, indent=2, sort_keys=True) + "\n").encode())
    return CaptureResult(requested_url=url, final_url=response["final_url"],
                         canonical_url=fields["canonical_url"], content_hash=content_hash,
                         content_type=response["content_type"], retrieved_at=observed_at,
                         object_ref=object_rel.as_posix(), revision_ref=revision_rel.as_posix(),
                         text=fields["text"], title=fields["title"], author=fields["author"],
                         language=fields["language"], license=fields["license"], tags=fields["tags"],
                         changed=content_hash != previous.get("content_hash"), state=state)


def result_dict(result: CaptureResult) -> dict:
    """JSON-friendly helper for callers and diagnostics."""
    return asdict(result)
