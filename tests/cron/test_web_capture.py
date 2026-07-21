from __future__ import annotations

import email.message
import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts/cron/web_capture.py"


def _load(monkeypatch):
    sys.modules.pop("web_capture", None)
    spec = importlib.util.spec_from_file_location("web_capture", MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["web_capture"] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_validate_url", lambda _url: None)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    return module


class Response:
    def __init__(self, body, *, url="https://news.example/article", content_type="text/html",
                 headers=None, status=200):
        self.body = body
        self.url = url
        self.status = status
        self.headers = {"Content-Type": content_type, **(headers or {})}

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


HTML = b"""<html lang="en"><head><title>Report</title>
<link rel="canonical" href="/canonical"><link rel="license" href="https://license.example/x">
<meta name="author" content="Research Team"><meta name="keywords" content="APT, malware">
</head><body><script>ignore me</script><article><h1>Report</h1><p>Material finding.</p></article></body></html>"""


def test_first_capture_is_immutable_and_metadata_rich(monkeypatch, tmp_path):
    m = _load(monkeypatch)
    result = m.capture(tmp_path, "https://news.example/article", native_id="native-1",
                       publisher="Example", observed_at="2026-07-18T10:00:00Z",
                       opener=lambda _req, _timeout: Response(
                           HTML, headers={"ETag": '"one"', "Last-Modified": "yesterday"}))

    assert result.changed
    assert result.canonical_url == "https://news.example/canonical"
    assert result.author == "Research Team" and result.language == "en"
    assert result.tags == ["APT", "malware"]
    assert "Material finding." in result.text and "ignore me" not in result.text
    assert (tmp_path / result.object_ref).read_bytes() == HTML
    record = json.loads((tmp_path / result.revision_ref).read_text())
    assert record["source_native_id"] == "native-1" and record["publisher"] == "Example"
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    m.capture(tmp_path, "https://news.example/article", native_id="native-1",
              publisher="Example", previous={}, observed_at="2026-07-18T10:00:00Z",
              opener=lambda _req, _timeout: Response(HTML))
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before


def test_not_modified_uses_previous_state_without_artifact(monkeypatch, tmp_path):
    m = _load(monkeypatch)
    previous = {"content_hash": "abc", "canonical_url": "https://news.example/canonical",
                "final_url": "https://news.example/article", "object_ref": "objects/a",
                "revision_ref": "revisions/a", "etag": '"one"', "content_type": "text/html"}

    def not_modified(_req, _timeout):
        raise urllib.error.HTTPError("https://news.example/article", 304, "", {}, None)

    result = m.capture(tmp_path, "https://news.example/article", previous=previous,
                       opener=not_modified)
    assert not result.changed and result.content_hash == "abc" and result.state is previous
    assert not list(tmp_path.rglob("*"))


def test_changed_content_creates_new_revision_and_object(monkeypatch, tmp_path):
    m = _load(monkeypatch)
    first = m.capture(tmp_path, "https://news.example/article", native_id="n",
                      opener=lambda _req, _timeout: Response(HTML))
    changed = m.capture(tmp_path, "https://news.example/article", native_id="n",
                        previous=first.state,
                        opener=lambda _req, _timeout: Response(HTML.replace(b"Material", b"Corrected")))
    assert changed.changed and changed.content_hash != first.content_hash
    assert changed.object_ref != first.object_ref and changed.revision_ref != first.revision_ref
    assert len(list((tmp_path / "objects").rglob("*.html"))) == 2


@pytest.mark.parametrize(("content_type", "body", "category"), [
    ("application/pdf", b"pdf", "unsupported-content"),
    ("text/html", b"<html><script>only script</script></html>", "extraction"),
])
def test_unsupported_and_empty_extraction_fail_loudly(monkeypatch, tmp_path,
                                                       content_type, body, category):
    m = _load(monkeypatch)
    with pytest.raises(m.CaptureError) as exc:
        m.capture(tmp_path, "https://news.example/article",
                  opener=lambda _req, _timeout: Response(body, content_type=content_type))
    assert exc.value.category == category
    ref = m.dead_letter(tmp_path, "https://news.example/article", "n", exc.value,
                        observed_at="2026-07-18T10:00:00Z")
    assert json.loads((tmp_path / ref).read_text())["category"] == category


def test_oversize_rejected_before_and_during_read(monkeypatch):
    m = _load(monkeypatch)
    monkeypatch.setattr(m, "MAX_BYTES", 4)
    with pytest.raises(m.CaptureError, match="Content-Length") as exc:
        m.fetch_document("https://news.example/a",
                         opener=lambda _req, _timeout: Response(
                             b"12345", headers={"Content-Length": "5"}))
    assert exc.value.category == "oversize"
    with pytest.raises(m.CaptureError, match="response exceeds"):
        m.fetch_document("https://news.example/a",
                         opener=lambda _req, _timeout: Response(b"12345"))


def test_retries_transient_http_and_sends_validators(monkeypatch):
    m = _load(monkeypatch)
    calls = []
    headers = email.message.Message()
    headers["Retry-After"] = "0"

    def opener(req, _timeout):
        calls.append({k.lower(): v for k, v in req.headers.items()})
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 503, "", headers, None)
        return Response(b"plain", content_type="text/plain")

    body, _meta = m.fetch_document("https://news.example/a",
                                   {"etag": '"x"', "last_modified": "y"}, opener=opener)
    assert body == b"plain" and len(calls) == 2
    assert calls[0]["if-none-match"] == '"x"' and calls[0]["if-modified-since"] == "y"


def test_upstream_removal_has_explicit_category(monkeypatch):
    m = _load(monkeypatch)

    def gone(req, _timeout):
        raise urllib.error.HTTPError(req.full_url, 410, "Gone", {}, None)

    with pytest.raises(m.CaptureError) as exc:
        m.fetch_document("https://news.example/a", opener=gone)
    assert exc.value.category == "upstream-removed"


def test_url_with_embedded_credentials_is_rejected(monkeypatch):
    m = _load(monkeypatch)
    # Exercise the real validator rather than the network-isolated test stub.
    monkeypatch.undo()
    with pytest.raises(m.CaptureError, match="must not contain credentials") as exc:
        m._validate_url("https://analyst:secret@news.example/article")
    assert exc.value.category == "invalid-url"


def test_dead_letter_is_idempotent(monkeypatch, tmp_path):
    m = _load(monkeypatch)
    error = m.CaptureError("network", "timeout")
    one = m.dead_letter(tmp_path, "https://news.example/a", "n", error, observed_at="first")
    two = m.dead_letter(tmp_path, "https://news.example/a", "n", error, observed_at="second")
    assert one == two
    assert json.loads((tmp_path / one).read_text())["observed_at"] == "first"
