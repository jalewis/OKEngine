"""deployment_checks.check_source_identity: identity drift counted, never repaired (okengine#886).

A vault held one article at up to 13 seats, 20 different papers under one placeholder URL, and
~600 importer-written pages with no `id` — none of it visible between reviews. This check makes the
class a standing WARN.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/cron/deployment_checks.py"
WRITE = REPO / "okengine-mcp/write_server.py"


def _mod():
    spec = importlib.util.spec_from_file_location("deployment_checks_source_identity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _page(root: Path, rel: str, head: str) -> None:
    p = root / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{head}---\nbody\n")


def _run(root: Path) -> str:
    c = _mod()
    c.configure(root, root / "data", root / "hermes")
    c.F.clear()
    c.check_source_identity()
    return "\n".join(f"{lvl} {area} {msg}" for lvl, area, msg in c.F)


def test_shared_urls_placeholders_and_idless_pages_are_counted(tmp_path):
    for n in ("a", "b"):
        _page(tmp_path, f"sources/{n}.md", f"id: sources:{n}\nurl: https://x.test/p/\n")
    _page(tmp_path, "sources/c.md", "id: sources:c\nurl: \"https://x.test/p\"\n")
    _page(tmp_path, "sources/ph.md", "id: sources:ph\nurl: https://x.test/abs/[pending]\n")
    _page(tmp_path, "sources/noid.md", "url: https://x.test/unique\n")
    _page(tmp_path, "entities/e.md", "type: entity\n")
    out = _run(tmp_path)
    assert "1 source URL(s) are held by more than one live page (3 pages)" in out
    assert "1 source page(s) carry a placeholder URL" in out
    assert "namespace 'sources' has 1 live page(s) with no `id`" in out
    assert "namespace 'entities' has 1 live page(s) with no `id`" in out
    assert all(line.startswith("WARN source-identity") for line in out.splitlines())


def test_a_clean_vault_raises_nothing(tmp_path):
    """NEGATIVE: distinct URLs, real ids, retired copies, generated and history files are fine."""
    _page(tmp_path, "sources/a.md", "id: sources:a\nurl: https://x.test/a\n")
    _page(tmp_path, "sources/b.md", "id: sources:b\nurl: https://x.test/b\n")
    _page(tmp_path, "sources/old.md", "status: tombstoned\nurl: https://x.test/a\n")
    _page(tmp_path, "sources/INDEX.md", "url: https://x.test/a\n")
    _page(tmp_path, "sources/_draft.md", "url: https://x.test/a\n")
    _page(tmp_path, "_logs/2026.md", "url: https://x.test/a\n")
    (tmp_path / "wiki" / "HOT.md").write_text("root file\n")
    (tmp_path / "wiki" / "sources" / "gone.md").symlink_to(tmp_path / "nowhere.md")
    _page(tmp_path, "sources/nourl.md", "id: sources:n\nurl: UNKNOWN\n")
    assert _run(tmp_path) == ""


def test_no_wiki_is_a_clean_skip(tmp_path):
    assert _run(tmp_path) == ""


@pytest.mark.parametrize("url", [
    "https://x.test/p", "https://x.test/p/", " https://x.test/p ", "http://a.test/", "/", "",
    "UNKNOWN", "null", "https://x.test/P", "ftp://x.test/p",
    # okengine#901: bare service endpoints carry no identity; a record query keeps it
    "https://gis.x.test/rest/services/P/MapServer/0/query",
    "https://gis.x.test/rest/services/P/MapServer/0/query?where=ID%3D1",
    "https://data.x.test/resource/ab12-cd34.json",
])
def test_the_grouping_key_equals_the_write_paths_url_identity(url, tmp_path, monkeypatch):
    """The detector and deduplication._norm_url both call state._source_identity_url; this pins
    them together so they never disagree about "the same URL"."""
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("types: {}\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    spec = importlib.util.spec_from_file_location("write_server_url_parity", WRITE)
    w = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = w
    try:
        spec.loader.exec_module(w)
        assert _mod()._source_url_key(url) == w._norm_url(url)
    finally:
        sys.modules.pop(spec.name, None)


ENDPOINT = "https://gis.x.test/arcgis/rest/services/Parcels/MapServer/0/query"


def test_bare_service_endpoint_urls_are_counted_never_grouped_as_shared(tmp_path):
    """okengine#901 detector: pages whose url is a bare API endpoint are where earlier converges
    fused unrelated records. They are counted (WARN) and are NOT reported as one shared document,
    which would point the operator at a same-URL MERGE of different records."""
    for n in ("a", "b", "c"):
        _page(tmp_path, f"sources/{n}.md", f"id: sources:{n}\nurl: \"{ENDPOINT}\"\n")
    _page(tmp_path, "sources/d.md", "id: sources:d\nurl: https://data.x.test/resource/ab12-cd34.json\n")
    _page(tmp_path, "sources/r1.md", f"id: sources:r1\nurl: {ENDPOINT}?where=ID%3D1\n")
    _page(tmp_path, "sources/r2.md", f"id: sources:r2\nurl: {ENDPOINT}?where=ID%3D2\n")
    out = _run(tmp_path)
    assert "4 source page(s) carry a bare service-endpoint URL" in out, out
    assert out.rstrip().endswith("e.g. sources/a.md, sources/b.md, sources/c.md"), (
        f"at most three examples: {out}")
    assert "held by more than one live page" not in out, out
    assert all(line.startswith("WARN source-identity") for line in out.splitlines())


def test_record_specific_query_urls_are_not_endpoint_findings(tmp_path):
    """NEGATIVE: the same path with a record query string is an ordinary identity."""
    _page(tmp_path, "sources/r1.md", f"id: sources:r1\nurl: {ENDPOINT}?where=ID%3D1\n")
    _page(tmp_path, "sources/r1b.md", f"id: sources:r1b\nurl: {ENDPOINT}?where=ID%3D1\n")
    out = _run(tmp_path)
    assert "service-endpoint" not in out, out
    assert "1 source URL(s) are held by more than one live page (2 pages)" in out, out


@pytest.mark.parametrize("script", ["reid_sources_by_url.py", "dedup_sources_by_url.py"])
@pytest.mark.parametrize("url", [
    ENDPOINT, ENDPOINT + "?where=ID%3D1", "https://x.test/p/", "UNKNOWN", "",
    "https://data.x.test/resource/ab12-cd34.json", "https://x.test/query-tips",
])
def test_operator_url_lanes_share_the_write_paths_identity_rule(script, url):
    """The re-id and dedup-by-url operator scripts used private copies of the rule; a copy without
    the endpoint rule would re-id or MERGE every record read from one endpoint as one document."""
    from okengine.write_services.state import _source_identity_url
    spec = importlib.util.spec_from_file_location(
        f"okengine901_{script[:-3]}", REPO / "scripts" / script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.norm_url(url) == _source_identity_url(url)
