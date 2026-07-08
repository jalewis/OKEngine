"""Reader input-shape / perf regressions (pre-release invariant audit).

#20: a vault page whose frontmatter `title`/`name` is a bare YAML-inferred date/int/list
     (natural for a year/annual-report entity) must EXPORT, not 500 — `.strip()` on the
     non-str value used to raise AttributeError on /api/download and in _clean_markdown.
#21: `_resolve_embeds` must MEMOIZE the full-vault rglob it runs for a basename embed that
     misses the flat path — otherwise every render re-walks the whole (sharded) vault once
     per embed.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("nh3")
pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "okengine-reader" / "app.py"


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    sys.path.insert(0, str(APP.parent))   # so app.py's `import limits` resolves
    sys.modules.pop("reader_app", None)
    spec = importlib.util.spec_from_file_location("reader_app", APP)
    m = importlib.util.module_from_spec(spec)
    sys.modules["reader_app"] = m
    spec.loader.exec_module(m)
    return m


# ── #20: non-string title/name doesn't 500 the download ─────────────────────
def test_download_survives_yaml_inferred_numeric_name(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki" / "entities" / "a"
    wiki.mkdir(parents=True)
    # `name: 2024` (unquoted, no title) — yaml SafeLoader infers int, not str.
    (wiki / "annual-2024.md").write_text(
        "---\ntype: report\nname: 2024\n---\n\nbody text\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)

    resp = m.api_download(request=None, fmt="md", path="entities/a/annual-2024")  # must not raise
    assert b"body text" in resp.body
    assert b"# 2024" in resp.body                       # coerced title used as H1


def test_clean_markdown_survives_yaml_date_title(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    raw = "---\ntitle: 2026-07-08\n---\n\nhello\n"       # yaml infers datetime.date
    out = m._clean_markdown(raw)                          # must not raise on .strip()
    assert out.lstrip().startswith("# 2026-07-08")


# ── #21: the unresolved-embed rglob is memoized across renders ──────────────
class _CountingWiki:
    """Proxy for the WIKI Path that counts rglob() calls (the expensive full-vault walk)."""
    def __init__(self, real):
        self._real = real
        self.rglob_calls = 0

    def rglob(self, pat):
        self.rglob_calls += 1
        return self._real.rglob(pat)

    def __truediv__(self, other):
        return self._real / other

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_unresolved_embed_rglob_is_cached(tmp_path, monkeypatch):
    ent = tmp_path / "wiki" / "entities" / "a"
    ent.mkdir(parents=True)
    (ent / "apt29.md").write_text("---\ntitle: APT29\n---\n\nembedded body\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)

    spy = _CountingWiki(m.WIKI)
    monkeypatch.setattr(m, "WIKI", spy)
    body = "see ![[apt29]]"                               # basename embed → misses flat path
    assert "embedded body" in m.render_md(body)           # resolves via rglob (walk #1)
    m.render_md(body)                                     # second render: served from cache
    assert spy.rglob_calls == 1                           # pre-fix: 2 (one walk per render)
