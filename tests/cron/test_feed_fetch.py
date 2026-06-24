import importlib.util
import sys
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
