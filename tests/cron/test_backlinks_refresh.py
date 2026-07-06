"""backlinks-refresh (okengine#168) — the precomputed backlink artifact.

Pins the canonical invert+filter+title semantics in backlink_lib (which the
reader's fallback path mirrors) and the refresh script's refusal to clobber a
good artifact with a bad iwe run.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
CRON = REPO / "scripts" / "cron"


def _load(name):
    sys.path.insert(0, str(CRON))
    spec = importlib.util.spec_from_file_location(name, CRON / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


lib = _load("backlink_lib")


def _vault(tmp_path, exclude=("operational",)):
    vault = tmp_path / "vault"
    wiki = vault / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (vault / "schema.yaml").write_text(
        "exclude:\n" + "".join(f"  - wiki/{e}/\n" for e in exclude),
        encoding="utf-8")
    return vault, wiki


# ── filter semantics ─────────────────────────────────────────────────────────
def test_excluded_top_dirs_reads_schema_and_adds_dashboards(tmp_path):
    vault, _ = _vault(tmp_path, exclude=("operational", "raw"))
    excl = lib.excluded_top_dirs(vault)
    # dashboards + sources are always-dropped defaults; operational/raw from schema exclude:
    assert {"operational", "raw", "dashboards", "sources"} <= excl


def test_backlink_drop_is_pack_configurable(tmp_path):
    # default (no backlink_drop key) -> sources dropped
    v_default, _ = _vault(tmp_path / "d", exclude=())
    assert "sources" in lib.excluded_top_dirs(v_default)
    # backlink_drop: [] -> sources RE-INCLUDED (the pack opt-in)
    v_incl = tmp_path / "i"; (v_incl / "wiki").mkdir(parents=True)
    (v_incl / "schema.yaml").write_text("backlink_drop: []\n")
    excl = lib.excluded_top_dirs(v_incl)
    assert "sources" not in excl and "dashboards" in excl  # dashboards always surfaced-derived
    # backlink_drop: [raw, foo] -> those dropped, sources NOT (override replaces the default)
    v_custom = tmp_path / "c"; (v_custom / "wiki").mkdir(parents=True)
    (v_custom / "schema.yaml").write_text("backlink_drop:\n  - raw\n  - wiki/foo/\n")
    excl2 = lib.excluded_top_dirs(v_custom)
    assert {"raw", "foo"} <= excl2 and "sources" not in excl2


def test_skip_source_reserved_and_excluded():
    excl = frozenset({"operational", "dashboards", "sources"})
    for key in ("entities/a/INDEX", "INDEX", "concepts/_draft", "HOT", "log",
                "operational/queue", "dashboards/kb-health", "sources/2026/report"):
        assert lib.skip_source(key, excl), key
    for key in ("entities/a/acme-corp", "briefings/daily", "concepts/x"):
        assert not lib.skip_source(key, excl), key


# ── titles ───────────────────────────────────────────────────────────────────
def test_page_title_precedence(tmp_path):
    _, wiki = _vault(tmp_path)
    (wiki / "a.md").write_text("---\ntitle: From Title\nname: N\n---\n# H1\n")
    (wiki / "b.md").write_text("---\nname: From Name\n---\nbody\n")
    (wiki / "c.md").write_text("---\ntype: source\n---\n# Real Headline\n## Summary\n")
    (wiki / "some-page-slug.md").write_text("no frontmatter, no h1\n")
    assert lib.page_title(wiki, "a") == "From Title"
    assert lib.page_title(wiki, "b") == "From Name"
    assert lib.page_title(wiki, "c") == "Real Headline"
    assert lib.page_title(wiki, "some-page-slug") == "some page slug"
    assert lib.page_title(wiki, "missing-file") == "missing file"


# ── inversion ────────────────────────────────────────────────────────────────
def _docs():
    return [
        {"key": "briefings/daily", "references": [  # gitleaks:allow — "key" is the iwe wikilink-map field
            {"key": "entities/a/acme"}, {"key": "entities/a/acme"},  # dup ref
            {"key": "briefings/daily"},                              # self-ref
        ]},
        {"key": "concepts/z", "references": [{"key": "entities/a/acme"}]},
        {"key": "sources/2026/x", "references": [{"key": "entities/a/acme"}]},   # source referrer -> DROPPED
        {"key": "dashboards/kb-health", "references": [{"key": "entities/a/acme"}]},
        {"key": "operational/queue", "references": [{"key": "entities/a/acme"}]},
        {"key": "entities/a/acme", "references": [{"key": "sources/2026/x"}]},   # source TARGET -> DROPPED
    ]


def test_invert_filters_dedupes_and_sorts(tmp_path):
    _, wiki = _vault(tmp_path)
    (wiki / "concepts").mkdir()
    (wiki / "briefings").mkdir()
    (wiki / "concepts/z.md").write_text("---\ntitle: Zeta Concept\n---\n")
    (wiki / "briefings/daily.md").write_text("---\ntitle: Alpha Brief\n---\n")
    excl = lib.excluded_top_dirs(_vault(tmp_path / "v2")[0])  # includes sources + dashboards
    bl = lib.invert(_docs(), wiki, excl)
    refs = bl["entities/a/acme"]
    # briefings + concepts kept (title-sorted, dup deduped); sources/dashboards/operational dropped
    assert [r["key"] for r in refs] == ["briefings/daily", "concepts/z"]
    assert refs[0]["title"] == "Alpha Brief"
    assert "sources/2026/x" not in bl          # source excluded as a TARGET too
    assert "sources" in excl                   # the new default exclusion


def test_sources_excluded_both_ways(tmp_path):
    """sources/ is dropped as BOTH referrer and target: a source citing an entity contributes
    no backlink, and an entity citing a source creates no target entry."""
    _, wiki = _vault(tmp_path)
    excl = lib.excluded_top_dirs(_vault(tmp_path / "v2")[0])
    assert "sources" in excl
    bl = lib.invert([
        {"key": "sources/2026/a", "references": [{"key": "entities/x"}]},   # source -> entity
        {"key": "briefings/b", "references": [{"key": "sources/2026/a"}]},  # brief -> source
    ], wiki, excl)
    assert "entities/x" not in bl        # source referrer dropped
    assert "sources/2026/a" not in bl    # source target dropped
    assert bl == {}


def test_build_artifact_meta(tmp_path):
    vault, wiki = _vault(tmp_path)
    (wiki / "briefings").mkdir()
    (wiki / "briefings/x.md").write_text("---\ntitle: X\n---\n")
    art = lib.build_artifact(
        [{"key": "briefings/x", "references": [{"key": "entities/a"}]}],
        wiki, vault, built_at=1000.5)
    assert art["version"] == lib.ARTIFACT_VERSION
    assert art["built_at"] == 1000
    assert art["pages"] == 1 and art["targets"] == 1 and art["edges"] == 1
    assert {"dashboards", "sources"} <= set(art["excluded_namespaces"])
    assert art["backlinks"]["entities/a"] == [{"key": "briefings/x", "title": "X"}]


def test_write_artifact_atomic_roundtrip(tmp_path):
    _, wiki = _vault(tmp_path)
    out = lib.write_artifact({"version": 1, "backlinks": {"a": []}}, wiki)
    assert out.name == ".backlinks.json"
    assert json.loads(out.read_text())["backlinks"] == {"a": []}
    assert not (wiki / ".backlinks.json.tmp").exists()


# ── the refresh script (scanner-backed, okengine#179) ────────────────────────
def _run_main(tmp_path, monkeypatch):
    mod = _load("backlinks_refresh")
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "vault"))
    return mod.main()


def test_main_writes_artifact(tmp_path, monkeypatch):
    vault, wiki = _vault(tmp_path)
    (wiki / "briefings").mkdir(); (wiki / "entities" / "a").mkdir(parents=True)
    (wiki / "entities/a/acme.md").write_text("---\ntitle: Acme\n---\n# Acme\n")
    (wiki / "briefings/x.md").write_text("# X\nSee [[entities/a/acme|Acme]].\n")
    code = _run_main(tmp_path, monkeypatch)
    assert code == 0
    art = json.loads((wiki / ".backlinks.json").read_text())
    assert art["backlinks"]["entities/a/acme"][0]["key"] == "briefings/x"


def test_main_refuses_empty_graph_and_keeps_artifact(tmp_path, monkeypatch):
    vault, wiki = _vault(tmp_path)
    (wiki / "briefings").mkdir(); (wiki / "entities" / "a").mkdir(parents=True)
    (wiki / "entities/a/acme.md").write_text("# Acme\n")
    (wiki / "briefings/x.md").write_text("# X\nSee [[entities/a/acme]].\n")
    assert _run_main(tmp_path, monkeypatch) == 0
    good = (wiki / ".backlinks.json").read_text()
    # now empty the vault of readable docs -> scan returns [] -> exit 2, keep the old artifact
    for md in wiki.rglob("*.md"):
        md.unlink()
    assert _run_main(tmp_path, monkeypatch) == 2
    assert (wiki / ".backlinks.json").read_text() == good


def test_main_missing_wiki_is_loud(tmp_path, monkeypatch):
    mod = _load("backlinks_refresh")
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "no-such-vault"))
    assert mod.main() == 2
    assert not (tmp_path / "no-such-vault" / "wiki" / ".backlinks.json").exists()


# ── vault resolution (okengine#168 follow-up) ────────────────────────────────
# Regression: cron-plus runs no_agent scripts from the scripts dir (NOT the declared
# workdir) and sets WIKI_PATH. The old cwd-first resolution therefore failed 'no wiki/'
# on every SCHEDULED run while passing every VAULT_DIR-setting unit test above.
def test_resolve_vault_prefers_wiki_path_over_cwd(tmp_path, monkeypatch):
    vault = tmp_path / "vault"; vault.mkdir()
    scripts = tmp_path / "scripts"; scripts.mkdir()   # mimics /opt/data/scripts
    mod = _load("backlinks_refresh")
    monkeypatch.delenv("VAULT_DIR", raising=False)
    monkeypatch.setenv("WIKI_PATH", str(vault))
    monkeypatch.chdir(scripts)                        # cwd is NOT the vault
    assert mod._resolve_vault() == vault.resolve()


def test_resolve_vault_dir_overrides_wiki_path(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"; explicit.mkdir()
    other = tmp_path / "wikipath"; other.mkdir()
    mod = _load("backlinks_refresh")
    monkeypatch.setenv("VAULT_DIR", str(explicit))
    monkeypatch.setenv("WIKI_PATH", str(other))
    assert mod._resolve_vault() == explicit.resolve()


def test_resolve_vault_falls_back_to_cwd(tmp_path, monkeypatch):
    cwd = tmp_path / "here"; cwd.mkdir()
    mod = _load("backlinks_refresh")
    monkeypatch.delenv("VAULT_DIR", raising=False)
    monkeypatch.delenv("WIKI_PATH", raising=False)
    monkeypatch.chdir(cwd)
    assert mod._resolve_vault() == cwd.resolve()


# ── forward-reference scan (okengine#179 — the iwe-parity link scanner) ───────
def _scan_vault(tmp_path):
    vault, wiki = _vault(tmp_path, exclude=("operational",))
    for d in ("entities/c", "entities/b", "concepts/b", "briefings", "sources/2026"):
        (wiki / d).mkdir(parents=True, exist_ok=True)
    (wiki / "entities/c/crowdstrike.md").write_text("---\ntitle: CrowdStrike\n---\n# CS\n")
    (wiki / "concepts/b/bitlocker.md").write_text("# Bit concept\n")
    (wiki / "entities/b/bitlocker.md").write_text("# Bit entity\n")   # basename collision
    return vault, wiki


def _refs(docs, key):
    return sorted(r["key"] for d in docs if d["key"] == key for r in d["references"])


def test_scan_wikilink_resolution_and_skips(tmp_path):
    vault, wiki = _scan_vault(tmp_path)
    (wiki / "briefings/b.md").write_text(
        "---\nsee_also: [\"[[entities/c/crowdstrike]]\"]\n---\n"           # frontmatter -> skip
        "Body [[entities/c/crowdstrike|CrowdStrike]] and `[[concepts/b/bitlocker]]` code-skip.\n"
        "```\n[[entities/c/crowdstrike]] fenced-skip\n```\n"
        "Path-hint ignored: [[wiki/x/crowdstrike]] resolves by basename.\n")
    docs = lib.scan_forward_refs(wiki, lib.excluded_top_dirs(vault))
    # crowdstrike (body wikilink + basename-resolved path-hint, deduped); NOT the frontmatter/code ones
    assert _refs(docs, "briefings/b") == ["entities/c/crowdstrike"]


def test_scan_collision_picks_alphabetically_first(tmp_path):
    vault, wiki = _scan_vault(tmp_path)
    (wiki / "briefings/b.md").write_text("# B\n[[bitlocker]] and [[wiki/entities/bitlocker]]\n")
    docs = lib.scan_forward_refs(wiki, lib.excluded_top_dirs(vault))
    # both links resolve by basename 'bitlocker'; collision -> concepts/ < entities/ (iwe parity)
    assert _refs(docs, "briefings/b") == ["concepts/b/bitlocker"]


def test_scan_multiline_label_and_dangling(tmp_path):
    vault, wiki = _scan_vault(tmp_path)
    (wiki / "briefings/b.md").write_text(
        "# B\nThe [[entities/c/crowdstrike|CrowdStrike\nEDR platform]] wraps a line.\n"
        "A [[entities/z/ghost|dangling]] link is kept.\n")
    docs = lib.scan_forward_refs(wiki, lib.excluded_top_dirs(vault))
    assert _refs(docs, "briefings/b") == ["entities/c/crowdstrike", "entities/z/ghost"]


def test_scan_skips_excluded_namespaces_as_referrers(tmp_path):
    vault, wiki = _scan_vault(tmp_path)
    # a sources/ page is NOT read (excluded) -> it never appears as a referrer doc
    (wiki / "sources/2026/x.md").write_text("# X\n[[entities/c/crowdstrike]]\n")
    docs = lib.scan_forward_refs(wiki, lib.excluded_top_dirs(vault))
    assert not any(d["key"] == "sources/2026/x" for d in docs)
