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


def test_real_url_validator_scheme_dns_private_and_public_paths(monkeypatch):
    m = _load(monkeypatch)
    monkeypatch.undo()
    with pytest.raises(m.CaptureError, match="scheme/host"):
        m._validate_url("file:///tmp/a")

    monkeypatch.setattr(m, "ALLOW_PRIVATE", True)
    m._validate_url("http://localhost/a")
    monkeypatch.setattr(m, "ALLOW_PRIVATE", False)
    monkeypatch.setattr(
        m.socket, "getaddrinfo",
        lambda *_a, **_k: (_ for _ in ()).throw(m.socket.gaierror("missing")),
    )
    with pytest.raises(m.CaptureError) as exc:
        m._validate_url("https://missing.example/a")
    assert exc.value.category == "dns"

    monkeypatch.setattr(
        m.socket, "getaddrinfo",
        lambda *_a, **_k: [(None, None, None, None, ("127.0.0.1", 443))],
    )
    with pytest.raises(m.CaptureError) as exc:
        m._validate_url("https://private.example/a")
    assert exc.value.category == "ssrf"
    monkeypatch.setattr(
        m.socket, "getaddrinfo",
        lambda *_a, **_k: [(None, None, None, None, ("8.8.8.8", 443))],
    )
    m._validate_url("https://public.example/a")


def test_redirect_default_open_and_retry_delay_edges(monkeypatch):
    m = _load(monkeypatch)
    validated = []
    monkeypatch.setattr(m, "_validate_url", lambda url: validated.append(url))

    class Parent:
        def redirect_request(self, *_args):
            return "redirected"

    monkeypatch.setattr(urllib.request.HTTPRedirectHandler, "redirect_request", Parent.redirect_request)
    assert m._SafeRedirect().redirect_request(None, None, 302, "", {}, "https://next") == "redirected"
    assert validated == ["https://next"]

    opened = []
    monkeypatch.setattr(
        urllib.request, "build_opener",
        lambda *_a: type("Opener", (), {"open": lambda _self, req, timeout: opened.append((req, timeout)) or "ok"})(),
    )
    assert m._default_open("request", 3) == "ok"
    assert opened == [("request", 3)]
    error = urllib.error.HTTPError("u", 503, "", {"Retry-After": "not-a-number"}, None)
    assert m._retry_after(error, 2) == 4.0


def test_fetch_fatal_http_and_network_retry_paths(monkeypatch):
    m = _load(monkeypatch)
    with pytest.raises(m.CaptureError) as exc:
        m.fetch_document(
            "https://news.example/a",
            opener=lambda req, _timeout: (_ for _ in ()).throw(
                urllib.error.HTTPError(req.full_url, 400, "Bad", {}, None)),
        )
    assert exc.value.category == "http"

    calls = []
    def eventually(req, _timeout):
        calls.append(req)
        if len(calls) < m.MAX_RETRIES:
            raise TimeoutError("slow")
        return Response(b"plain", content_type="text/plain")
    assert m.fetch_document("https://news.example/a", opener=eventually)[0] == b"plain"

    with pytest.raises(m.CaptureError) as exc:
        m.fetch_document(
            "https://news.example/a",
            opener=lambda *_a: (_ for _ in ()).throw(OSError("offline")),
        )
    assert exc.value.category == "network"


def test_extraction_plain_metadata_and_parser_failure(monkeypatch):
    m = _load(monkeypatch)
    assert m.extract(b"  plain text  ", "text/plain", "https://x")["text"] == "plain text"
    html = b"""<html><head><meta property='og:locale' content='fr_FR'>
    <meta property='article:author' content='Author'><meta property='article:tag' content='one,two'>
    <meta name='description' content='ignored'>
    </head><body><div>text</div></body></html>"""
    fields = m.extract(html, "text/html", "https://x")
    assert fields["author"] == "Author" and fields["language"] == "fr_FR"
    assert fields["tags"] == ["one", "two"]

    monkeypatch.setattr(m._HTMLText, "feed", lambda *_a: (_ for _ in ()).throw(RuntimeError("parse")))
    with pytest.raises(m.CaptureError) as exc:
        m.extract(b"<p>x</p>", "text/html", "https://x")
    assert exc.value.category == "extraction"


def test_result_dict_returns_dataclass_fields(monkeypatch, tmp_path):
    m = _load(monkeypatch)
    result = m.capture(
        tmp_path, "https://news.example/a",
        opener=lambda *_a: Response(b"plain", content_type="text/plain"),
    )
    assert m.result_dict(result)["content_hash"] == result.content_hash


# ── okengine#748: revision identity is the extracted TEXT, not the fetched bytes ──────────
#
# A live vault accumulated 82,769 raw files carrying 6,531 distinct articles — 639 copies of
# one blog post — because `content_hash` covered the whole fetched document. Publishers whose
# markup carries a rotating token or timestamp therefore looked "changed" on every fetch, about
# ten times a day, forever. Consecutive captures of the worst offender differed only in
# `content_hash` and `fetched`; the extracted article was byte-identical.

CHURN_TEMPLATE = b"""<html lang="en"><head><title>Stable Article</title>
<link rel="canonical" href="https://news.example/stable"></head>
<body><!-- build-token: %s --><p>The article body never changes.</p>
<span class="sidebar">%s</span></body></html>"""


def _churn(token: bytes, sidebar: bytes = b"unchanging furniture"):
    return CHURN_TEMPLATE % (token, sidebar)


def test_markup_churn_with_an_unchanged_article_is_not_a_revision(tmp_path, monkeypatch):
    """THE REGRESSION. Same article, different markup token: `changed` must be False so no new
    raw item is minted."""
    m = _load(monkeypatch)
    first = _churn(b"aaaa")
    second = _churn(b"bbbb")
    assert first != second, "the fixture must actually differ in bytes"

    monkeypatch.setattr(m, "_default_open", lambda *_a, **_k: Response(first))
    one = m.capture(tmp_path, "https://news.example/stable")
    assert one.changed is True                      # nothing prior -> a first capture

    monkeypatch.setattr(m, "_default_open", lambda *_a, **_k: Response(second))
    two = m.capture(tmp_path, "https://news.example/stable", previous=one.state)

    assert two.content_hash != one.content_hash, "the bytes did change"
    assert two.text_hash == one.text_hash, "the article did not"
    assert two.changed is False, "a markup-only difference must not count as a revision"


def test_a_real_body_edit_is_still_a_revision(tmp_path, monkeypatch):
    """The gate must not have been loosened into uselessness."""
    m = _load(monkeypatch)
    monkeypatch.setattr(m, "_default_open",
                        lambda *_a, **_k: Response(_churn(b"aaaa", b"original text")))
    one = m.capture(tmp_path, "https://news.example/stable")
    monkeypatch.setattr(m, "_default_open",
                        lambda *_a, **_k: Response(_churn(b"aaaa", b"rewritten text")))
    two = m.capture(tmp_path, "https://news.example/stable", previous=one.state)
    assert two.text_hash != one.text_hash
    assert two.changed is True


def test_markup_churn_does_not_append_a_new_revision_artifact(tmp_path, monkeypatch):
    """The store-level half: 100 fetches of a churning page leave ONE revision record.

    Before the fix each fetch wrote its own revisions/ entry, which is what let a single
    article reach 639 of them."""
    m = _load(monkeypatch)
    for i in range(100):
        monkeypatch.setattr(m, "_default_open",
                            lambda *_a, _b=f"tok{i}".encode(), **_k: Response(_churn(_b)))
        m.capture(tmp_path, "https://news.example/stable")
    revisions = list((tmp_path / "revisions").rglob("*.json"))
    assert len(revisions) == 1, f"expected one revision, found {len(revisions)}"


def test_byte_identical_responses_still_share_one_object(tmp_path, monkeypatch):
    """The object store must stay BYTE addressed — moving revision identity to text must not
    make identical responses stop sharing a blob."""
    m = _load(monkeypatch)
    body = _churn(b"fixed")
    monkeypatch.setattr(m, "_default_open", lambda *_a, **_k: Response(body))
    m.capture(tmp_path, "https://news.example/stable")
    m.capture(tmp_path, "https://news.example/stable")
    assert len(list((tmp_path / "objects").rglob("*.html"))) == 1


def test_differing_markup_keeps_its_own_object_blob(tmp_path, monkeypatch):
    """Two distinct responses are two distinct blobs even when they extract to one article —
    the raw bytes remain recoverable for audit."""
    m = _load(monkeypatch)
    for token in (b"aaaa", b"bbbb"):
        monkeypatch.setattr(m, "_default_open", lambda *_a, _t=token, **_k: Response(_churn(_t)))
        m.capture(tmp_path, "https://news.example/stable")
    assert len(list((tmp_path / "objects").rglob("*.html"))) == 2
    assert len(list((tmp_path / "revisions").rglob("*.json"))) == 1


def test_text_fingerprint_normalizes_whitespace(monkeypatch):
    """Reflow and indentation churn must not read as an edit either."""
    m = _load(monkeypatch)
    assert m.text_fingerprint("a  b\n c") == m.text_fingerprint("a b c")
    assert m.text_fingerprint("a b") != m.text_fingerprint("a c")


def test_state_without_a_text_hash_falls_back_to_the_byte_comparison(tmp_path, monkeypatch):
    """Upgrade path. State written before the fix carries no text_hash; that one transition
    uses the old comparison, after which the page's state carries a text_hash and goes quiet.
    Treating absent-as-unchanged would swallow a real edit made across the upgrade."""
    m = _load(monkeypatch)
    monkeypatch.setattr(m, "_default_open", lambda *_a, **_k: Response(_churn(b"new")))
    legacy = {"content_hash": "0" * 64}             # pre-fix shape: no text_hash
    result = m.capture(tmp_path, "https://news.example/stable", previous=legacy)
    assert result.changed is True
    assert result.state["text_hash"], "state must carry a text_hash going forward"

    quiet = m.capture(tmp_path, "https://news.example/stable", previous=result.state)
    assert quiet.changed is False, "the very next fetch must already be stable"
