import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "feed_fetch.py"


def _load(monkeypatch, allow_private: bool = False):
    if allow_private:
        monkeypatch.setenv("FEED_ALLOW_PRIVATE_NETS", "1")
    else:
        monkeypatch.delenv("FEED_ALLOW_PRIVATE_NETS", raising=False)
    sys.modules.pop("feed_fetch", None)
    spec = importlib.util.spec_from_file_location("feed_fetch", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["feed_fetch"] = m
    spec.loader.exec_module(m)
    return m


def test_fetch_refuses_loopback_by_default(monkeypatch):
    m = _load(monkeypatch)
    with pytest.raises(ValueError) as ei:
        m.fetch("http://127.0.0.1:8080/feed.xml")
    assert "private/link-local" in str(ei.value)


def test_fetch_refuses_non_http_scheme(monkeypatch):
    m = _load(monkeypatch)
    with pytest.raises(ValueError) as ei:
        m.fetch("file:///etc/passwd")
    assert "unsupported" in str(ei.value)


def test_feed_xml_rejects_dtd_and_oversized_documents(monkeypatch):
    m = _load(monkeypatch)
    malicious = b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss><channel>&x;</channel></rss>'
    with pytest.raises(m.ET.ParseError, match="DTD/entity"):
        m._safe_xml_fromstring(malicious)
    with pytest.raises(m.ET.ParseError, match="10 MiB"):
        m._safe_xml_fromstring(b" " * (m.MAX_XML_BYTES + 1))


# ─── conditional GET + backoff (okengine#2) ────────────────────────────

import email.message  # noqa: E402
import urllib.error  # noqa: E402


class _FakeResp:
    def __init__(self, body=b"<rss><channel></channel></rss>", headers=None):
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, retry_after=None):
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError("http://feeds.example.test/rss", code, "err", hdrs, None)


def test_fetch_sends_conditional_headers(monkeypatch):
    m = _load(monkeypatch, allow_private=True)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return _FakeResp()

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    m.fetch("http://feeds.example.test/rss",
            {"etag": '"abc"', "last_modified": "Wed, 01 Jan 2025 00:00:00 GMT"})
    assert captured["headers"]["if-none-match"] == '"abc"'
    assert captured["headers"]["if-modified-since"] == "Wed, 01 Jan 2025 00:00:00 GMT"


def test_fetch_returns_validators(monkeypatch):
    m = _load(monkeypatch, allow_private=True)
    resp = _FakeResp(headers={"ETag": '"xyz"', "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT"})
    monkeypatch.setattr(m.urllib.request, "urlopen", lambda req, timeout=None: resp)
    body, validators = m.fetch("http://feeds.example.test/rss")
    assert b"channel" in body
    assert validators == {"etag": '"xyz"', "last_modified": "Wed, 01 Jan 2025 00:00:00 GMT"}


def test_fetch_304_raises_not_modified(monkeypatch):
    m = _load(monkeypatch, allow_private=True)

    def raise_304(req, timeout=None):
        raise _http_error(304)

    monkeypatch.setattr(m.urllib.request, "urlopen", raise_304)
    with pytest.raises(m.NotModified):
        m.fetch("http://feeds.example.test/rss", {"etag": '"abc"'})


def test_fetch_retries_on_503_then_succeeds(monkeypatch):
    m = _load(monkeypatch, allow_private=True)
    slept = []
    monkeypatch.setattr(m.time, "sleep", lambda s: slept.append(s))
    calls = {"n": 0}

    def flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(503, retry_after="0")
        return _FakeResp()

    monkeypatch.setattr(m.urllib.request, "urlopen", flaky)
    body, _ = m.fetch("http://feeds.example.test/rss")
    assert calls["n"] == 2          # retried once
    assert slept == [0.0]           # honored Retry-After: 0
    assert b"channel" in body


def test_fetch_gives_up_after_max_retries(monkeypatch):
    m = _load(monkeypatch, allow_private=True)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    def always_503(req, timeout=None):
        raise _http_error(503, retry_after="0")

    monkeypatch.setattr(m.urllib.request, "urlopen", always_503)
    with pytest.raises(urllib.error.HTTPError):
        m.fetch("http://feeds.example.test/rss")


def test_retry_after_caps_and_parses(monkeypatch):
    m = _load(monkeypatch, allow_private=True)
    assert m._retry_after(_http_error(503, retry_after="5"), 0) == 5.0
    assert m._retry_after(_http_error(503, retry_after="9999"), 0) == float(m.MAX_BACKOFF_S)
    # no Retry-After -> exponential backoff, capped
    assert m._retry_after(_http_error(503), 1) == 2.0


def test_empty_opml_is_clean_noop(monkeypatch, tmp_path):
    """Empty/absent feeds.opml is the safe out-of-the-box default (crons ship
    enabled, feeds ship empty). main() must return 0, not 1 — otherwise the
    scheduled feed-fetch cron logs a failure every run on a fresh install."""
    m = _load(monkeypatch)
    opml = tmp_path / "feeds.opml"
    opml.write_text('<?xml version="1.0"?><opml version="2.0"><head>'
                    '<title>t</title></head><body></body></opml>')
    rc = m.main(["--opml", str(opml), "--out-dir", str(tmp_path / "out"),
                 "--state", str(tmp_path / "s.json"), "--source-tag", "test"])
    assert rc == 0
    # absent file is also a no-op (returns 0), not a crash
    rc2 = m.main(["--opml", str(tmp_path / "missing.opml"),
                  "--out-dir", str(tmp_path / "out"), "--source-tag", "test"])
    assert rc2 == 0


def test_feed_attempts_record_success_failure_and_source_class(monkeypatch, tmp_path):
    m = _load(monkeypatch)
    opml = tmp_path / "feeds.opml"
    opml.write_text('<?xml version="1.0"?><opml><body>'
                    '<outline text="Official" xmlUrl="https://official.test/rss" '
                    'sourceKind="primary" independentOrigin="true"/>'
                    '<outline text="Broken" xmlUrl="https://broken.test/rss"/>'
                    '</body></opml>')
    rss = b'<rss><channel><item><guid>1</guid><title>One</title>' \
          b'<pubDate>Sat, 18 Jul 2026 10:00:00 GMT</pubDate></item></channel></rss>'

    def fetch(url, _validators=None):
        if "broken" in url:
            raise TimeoutError("private details must not enter telemetry")
        return rss, {"etag": "private-checkpoint"}

    monkeypatch.setattr(m, "fetch", fetch)
    ledger = tmp_path / "ledger"
    assert m.main(["--opml", str(opml), "--out-dir", str(tmp_path / "raw"),
                   "--state", str(tmp_path / "state.json"),
                   "--collection-ledger", str(ledger)]) == 0
    attempts = m.collection_ledger.load_attempts(ledger,
        now=datetime.now(timezone.utc))
    assert [row["outcome"] for row in attempts] == ["success", "failure"]
    assert attempts[0]["fetched"] == 1 and attempts[0]["accepted"] == 1
    assert attempts[1]["error_category"] == "timeout"
    assert "private details" not in next(ledger.glob("attempts-*.ndjson")).read_text()
    sources = m.collection_ledger.load_sources(ledger)
    official = next(row for row in sources if row["label"] == "Official")
    assert official["source_kind"] == "primary" and official["independent_origin"] is True


def test_raw_feed_landing_uses_channel_not_final_source_kind(monkeypatch, tmp_path):
    """Raw feed landing files should not carry a domain final-classification value.
    The ingest step assigns source_kind from the pack schema."""
    m = _load(monkeypatch)
    out = tmp_path / "raw"
    p = m.write_item(out, "Example Feed", {
        "title": "Example Item",
        "link": "https://example.test/item",
        "summary": "Short summary",
        "published": "2026-06-23T12:00:00+00:00",
    }, "test")
    text = p.read_text(encoding="utf-8")
    assert "source_channel: feed" in text
    assert "source_kind: feed" not in text


def test_same_title_items_do_not_overwrite_each_other(monkeypatch, tmp_path):
    m = _load(monkeypatch)
    out = tmp_path / "raw"
    base = {"title": "Same title", "summary": "x", "published": "2026-07-18T10:00:00Z"}
    one = m.write_item(out, "Feed", {**base, "id": "one", "link": "https://x.test/1"}, "")
    two = m.write_item(out, "Feed", {**base, "id": "two", "link": "https://x.test/2"}, "")
    assert one != two and one.exists() and two.exists()


def test_json_feed_preserves_rich_item_metadata(monkeypatch):
    m = _load(monkeypatch)
    items = m.parse_items(b'''{
      "version":"https://jsonfeed.org/version/1.1", "language":"en",
      "items":[{"id":"n1","url":"https://example.test/a","title":"A",
        "content_text":"Body","date_modified":"2026-07-18T10:00:00Z",
        "authors":[{"name":"Analyst"}],"tags":["APT"],
        "attachments":[{"url":"https://example.test/a.pdf"}]}]}''', 5)
    assert items[0]["authors"] == ["Analyst"]
    assert items[0]["tags"] == ["APT"] and items[0]["language"] == "en"
    assert items[0]["enclosures"] == ["https://example.test/a.pdf"]


def test_full_text_capture_is_opt_in_and_revisions_land_separately(monkeypatch, tmp_path):
    m = _load(monkeypatch)
    opml = tmp_path / "feeds.opml"
    opml.write_text('<?xml version="1.0"?><opml><body><outline text="Example" '
                    'xmlUrl="https://feed.example/rss"/></body></opml>')
    rss = b'''<rss><channel><item><guid>n1</guid><title>Report</title>
      <link>https://news.example/a</link><description>Summary</description>
      <pubDate>Fri, 18 Jul 2026 10:00:00 GMT</pubDate></item></channel></rss>'''
    monkeypatch.setattr(m, "fetch", lambda _url, _state=None: (rss, {}))
    hashes = iter(["a" * 64, "b" * 64])

    def fake_capture(_root, url, *, native_id, publisher, previous, observed_at):
        digest = next(hashes)
        state = {"content_hash": digest, "canonical_url": url, "final_url": url,
                 "object_ref": f"objects/{digest}.html", "revision_ref": f"revisions/{digest}.json"}
        return m.web_capture.CaptureResult(url, url, url, digest, "text/html", observed_at,
                                           state["object_ref"], state["revision_ref"],
                                           f"full text {digest[0]}", "Report", "", "", "", [],
                                           digest != previous.get("content_hash"), state)

    monkeypatch.setattr(m.web_capture, "capture", fake_capture)
    args = ["--opml", str(opml), "--out-dir", str(tmp_path / "raw"),
            "--state", str(tmp_path / "state.json"), "--capture-full-text"]
    assert m.main(args) == 0
    assert m.main(args) == 0
    pages = sorted((tmp_path / "raw").glob("*.md"))
    assert len(pages) == 2 and any("revision-" in page.name for page in pages)
    initial = next(page for page in pages if "revision-" not in page.name)
    assert "## Captured content" in initial.read_text()
    assert "content_hash: " + "a" * 64 in initial.read_text()


def test_feed_metadata_correction_creates_revision_when_bytes_are_unchanged(monkeypatch, tmp_path):
    m = _load(monkeypatch)
    opml = tmp_path / "feeds.opml"
    opml.write_text('<?xml version="1.0"?><opml><body><outline text="Example" '
                    'xmlUrl="https://feed.example/rss"/></body></opml>')
    titles = iter(("Report", "Correction: Report"))

    def fake_fetch(_url, _state=None):
        title = next(titles)
        return (f'''<rss><channel><item><guid>n1</guid><title>{title}</title>
          <link>https://news.example/a</link><description>Summary</description>
          <pubDate>Fri, 18 Jul 2026 10:00:00 GMT</pubDate></item></channel></rss>'''.encode(), {})

    def unchanged_capture(_root, url, *, native_id, publisher, previous, observed_at):
        digest = "a" * 64
        state = {"content_hash": digest, "canonical_url": url, "final_url": url,
                 "object_ref": f"objects/{digest}.html",
                 "revision_ref": f"revisions/{digest}.json"}
        return m.web_capture.CaptureResult(url, url, url, digest, "text/html", observed_at,
                                           state["object_ref"], state["revision_ref"], "same text",
                                           "Report", "", "", "", [], not previous, state)

    monkeypatch.setattr(m, "fetch", fake_fetch)
    monkeypatch.setattr(m.web_capture, "capture", unchanged_capture)
    args = ["--opml", str(opml), "--out-dir", str(tmp_path / "raw"),
            "--state", str(tmp_path / "state.json"), "--capture-full-text"]
    assert m.main(args) == 0
    assert m.main(args) == 0
    pages = sorted((tmp_path / "raw").glob("*.md"))
    assert len(pages) == 2
    correction = next(page for page in pages if "revision-" in page.name).read_text()
    assert "source_correction: true" in correction
    assert "content_hash: " + "a" * 64 in correction


def test_upstream_removal_after_capture_creates_retraction_revision(monkeypatch, tmp_path):
    m = _load(monkeypatch)
    opml = tmp_path / "feeds.opml"
    opml.write_text('<?xml version="1.0"?><opml><body><outline text="Example" '
                    'xmlUrl="https://feed.example/rss"/></body></opml>')
    rss = b'''<rss><channel><item><guid>n1</guid><title>Report</title>
      <link>https://news.example/a</link><description>Summary</description>
      <pubDate>Fri, 18 Jul 2026 10:00:00 GMT</pubDate></item></channel></rss>'''
    monkeypatch.setattr(m, "fetch", lambda _url, _state=None: (rss, {}))
    calls = {"count": 0}

    def capture_then_remove(_root, url, *, native_id, publisher, previous, observed_at):
        calls["count"] += 1
        if calls["count"] >= 2:
            raise m.web_capture.CaptureError("upstream-removed", "HTTP 410")
        digest = "a" * 64
        state = {"content_hash": digest, "canonical_url": url, "final_url": url,
                 "object_ref": f"objects/{digest}.html",
                 "revision_ref": f"revisions/{digest}.json"}
        return m.web_capture.CaptureResult(url, url, url, digest, "text/html", observed_at,
                                           state["object_ref"], state["revision_ref"], "text",
                                           "Report", "", "", "", [], True, state)

    monkeypatch.setattr(m.web_capture, "capture", capture_then_remove)
    args = ["--opml", str(opml), "--out-dir", str(tmp_path / "raw"),
            "--state", str(tmp_path / "state.json"), "--capture-full-text"]
    assert m.main(args) == 0
    assert m.main(args) == 0
    assert m.main(args) == 0
    pages = sorted((tmp_path / "raw").glob("*.md"))
    assert len(pages) == 2
    removed = next(page for page in pages if "revision-" in page.name).read_text()
    assert "source_retraction: true" in removed
    assert "capture_dead_letter:" in removed
