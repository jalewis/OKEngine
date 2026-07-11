"""find_references / retrieve_context must serve the cron-precomputed backlink artifact, not rebuild
the IWE graph live per call.

Regression: the read-MCP shelled out to kb_graph -> iwe on every call, rebuilding the whole graph —
O(vault size). On cyber-market's 60k-page vault that blew past the MCP call timeout (recurring
find_references timeouts). Now it reads wiki/.backlinks.json (O(dict lookup)); live IWE is the
fallback only when the artifact is absent/stale.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent
SRV = REPO / "okengine-mcp" / "server.py"


def _load(vault, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(vault))
    sys.path.insert(0, str(SRV.parent))
    sys.modules.pop("server", None)
    spec = importlib.util.spec_from_file_location("server", SRV)
    m = importlib.util.module_from_spec(spec)
    sys.modules["server"] = m
    spec.loader.exec_module(m)
    return m


def test_read_safe_appends_md_and_does_not_truncate_dotted_slug(tmp_path, monkeypatch):
    """Read-path _safe .md handling (invariant-audit M16 round-2): the read MCP must resolve a dotted
    slug to the SAME file the write path stores ('openssl-3.0.7-advisory.md'), not with_suffix()-
    truncate at the last dot to a nonexistent 'openssl-3.0.md' and 404 the page. This pins ONLY the
    .md-append agreement — the write path additionally applies _normalize_entity_shard / over-qualified
    and wiki/ prefix stripping that this read _safe does not (a pre-existing read/write divergence
    tracked separately for v0.11.1, not introduced here)."""
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    good = m._safe("sources/2026/07/openssl-3.0.7-advisory")
    assert good is not None, "dotted slug must resolve, not escape/None"
    assert good.name == "openssl-3.0.7-advisory.md", good          # full stem + .md
    assert "openssl-3.0.md" not in str(good)                       # NOT truncated at the last dot
    # an already-.md path is unchanged (no double .md)
    assert m._safe("entities/a/apt.md").name == "apt.md"


def _mk_vault(vault):
    w = vault / "wiki"
    (w / "entities" / "a").mkdir(parents=True)
    (w / "entities" / "a" / "apt.md").write_text(
        "---\ntype: actor\n---\nApt uses [[entities/m/mirai]] and [[concepts/c2]].\n", encoding="utf-8")
    (w / ".backlinks.json").write_text(json.dumps({
        "backlinks": {"entities/a/apt": [
            {"key": "sources/s1", "title": "Source One"},
            {"key": "briefings/b1", "title": "Brief One"}]}}), encoding="utf-8")


def test_find_references_serves_artifact_not_iwe(tmp_path, monkeypatch):
    _mk_vault(tmp_path)
    m = _load(tmp_path, monkeypatch)
    m._run = lambda *a, **k: "IWE-FALLBACK"                     # sentinel: fired only on live-IWE fallback
    out = m.find_references("entities/a/apt")
    assert "IWE-FALLBACK" not in out                            # served from the artifact, no subprocess
    assert "Referenced by (2)" in out
    assert "sources/s1" in out and "Source One" in out          # backlinks from the artifact
    assert "entities/m/mirai" in out and "concepts/c2" in out   # forward refs parsed from the page body
    assert "sources/s1" in m.find_references("apt")             # resolves a bare name too


def test_retrieve_context_serves_artifact(tmp_path, monkeypatch):
    _mk_vault(tmp_path)
    m = _load(tmp_path, monkeypatch)
    m._run = lambda *a, **k: "IWE-FALLBACK"
    m._authorize_read = lambda *a, **k: True
    out = m.retrieve_context("entities/a/apt")
    assert "IWE-FALLBACK" not in out and "Apt uses" in out      # page body + no IWE
    assert "Incoming backlinks (2)" in out and "Outbound references (2)" in out


def test_falls_back_to_iwe_when_artifact_absent(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir(parents=True)
    m = _load(tmp_path, monkeypatch)
    m._run = lambda *a, **k: "IWE-FALLBACK"
    assert m.find_references("anything") == "IWE-FALLBACK"      # no artifact -> live IWE fallback
