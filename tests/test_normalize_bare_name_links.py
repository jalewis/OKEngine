"""okengine#196: normalize_bare_name_links.build_index must not crash when an entity page carries a
SCALAR `aliases` string (a list field written as a comma-string). Before the fix it did
`[name] + (aliases or []) + [stem]`, so a scalar `aliases` raised `TypeError: list + str` and killed
the whole bare-name-normalization lane (fleet-health red). It now coerces to a list and still indexes
the alias — defense in depth alongside the write-path coercion."""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "scripts" / "cron" / "normalize_bare_name_links.py"


def _load(wiki_root, monkeypatch):
    # module-level VAULT/WIKI/ENT_DIR read WIKI_PATH at import — set env, then load fresh.
    monkeypatch.setenv("WIKI_PATH", str(wiki_root))
    monkeypatch.setenv("NORMALIZE_LINKS_DRY_RUN", "1")
    sys.modules.pop("normalize_bare_name_links", None)
    spec = importlib.util.spec_from_file_location("normalize_bare_name_links", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["normalize_bare_name_links"] = m
    spec.loader.exec_module(m)
    return m


def _entity(root, shard, slug, fm_body):
    d = root / "wiki" / "entities" / shard
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.md").write_text(f"---\n{fm_body}\n---\n# {slug}\n", encoding="utf-8")


def test_build_index_survives_scalar_aliases(tmp_path, monkeypatch):
    _entity(tmp_path, "s", "stealc", "name: StealC\naliases: StealC, StealC info-stealer")
    m = _load(tmp_path, monkeypatch)
    name_index, _valid = m.build_index()                      # must NOT raise
    assert "entities/s/stealc" in name_index[m._norm("StealC")]
    assert "entities/s/stealc" in name_index[m._norm("StealC info-stealer")]


def test_build_index_handles_list_and_missing_aliases(tmp_path, monkeypatch):
    _entity(tmp_path, "q", "qilin", "name: Qilin\naliases: [Agenda, Water Qilin]")
    _entity(tmp_path, "n", "noalias", "name: NoAlias")        # aliases absent
    m = _load(tmp_path, monkeypatch)
    name_index, _valid = m.build_index()
    assert "entities/q/qilin" in name_index[m._norm("Agenda")]
    assert "entities/q/qilin" in name_index[m._norm("Water Qilin")]
    assert "entities/n/noalias" in name_index[m._norm("NoAlias")]
