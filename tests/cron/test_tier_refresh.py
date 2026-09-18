"""tier_refresh — tier-distribution dashboard. Regression for the walk-up sub-domain blindspot."""
import importlib.util
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def _load(tmp, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp))
    sys.path.insert(0, str(REPO / "scripts" / "cron"))   # tier_refresh imports tier_lib as a sibling
    sys.modules.pop("tier_refresh", None)
    spec = importlib.util.spec_from_file_location("tier_refresh", REPO / "scripts/cron/tier_refresh.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["tier_refresh"] = m
    spec.loader.exec_module(m)
    return m


def test_namespace_bases_includes_walkup_subdomains(tmp_path, monkeypatch):  # invariant-audit #26
    """The tier count must include wiki/<subdomain>/<ns> (a walk-up sub-domain carries its own
    schema.yaml), else a co-installed vault under-counts every namespace and the operator sees a
    vault smaller than it is. A plain dir with no schema.yaml is NOT a sub-domain and is excluded."""
    w = tmp_path / "wiki"
    (w / "entities").mkdir(parents=True)
    (w / "acme").mkdir()
    (w / "acme" / "schema.yaml").write_text("okf: {}\n")     # marks 'acme' a walk-up sub-domain
    (w / "beta").mkdir()                                      # NOT a sub-domain (no schema.yaml)
    m = _load(tmp_path, monkeypatch)
    bases = [b.as_posix() for b in m._namespace_bases("entities")]
    assert any(b.endswith("wiki/entities") for b in bases), bases          # root namespace
    assert any(b.endswith("wiki/acme/entities") for b in bases), bases     # sub-domain namespace
    assert not any("beta" in b for b in bases), bases                       # non-subdomain excluded


def test_count_namespace_sums_root_and_subdomain(tmp_path, monkeypatch):
    """End-to-end: a namespace with pages in BOTH the root and a sub-domain counts all of them."""
    w = tmp_path / "wiki"
    (w / "entities" / "a").mkdir(parents=True)
    (w / "entities" / "a" / "root1.md").write_text("---\ntype: entity\n---\nx\n")
    (w / "acme").mkdir()
    (w / "acme" / "schema.yaml").write_text("okf: {}\n")
    (w / "acme" / "entities" / "b").mkdir(parents=True)
    (w / "acme" / "entities" / "b" / "sub1.md").write_text("---\ntype: entity\n---\ny\n")
    m = _load(tmp_path, monkeypatch)
    # entities is a status/path-neutral namespace here; tier_of buckets everything into one tier or
    # _untiered — either way the TOTAL page count must be 2 (root + sub-domain), never 1.
    counts = m._count_namespace("entities", {}, {"namespaces": {"entities": {}}}, m.datetime.now(m.timezone.utc).date())
    total = sum(v for k, v in counts.items() if not k.startswith("_")) + counts.get("_untiered", 0)
    assert total == 2, counts


def test_main_writes_distribution_and_deltas(tmp_path, monkeypatch, capsys):
    wiki = tmp_path / "wiki"
    (wiki / "sources" / "2026" / "07").mkdir(parents=True)
    (wiki / "sources" / "2026" / "07" / "new.md").write_text("---\ntype: source\n---\n")
    m = _load(tmp_path, monkeypatch)
    cfg = {
        "hot_days": 30, "warm_days": 365,
        "namespaces": {"sources": {"from_path": True}},
    }
    monkeypatch.setattr(m.tier_lib, "load_cfg", lambda _vault: cfg)
    monkeypatch.setattr(m.tz_lib, "deployment_today", lambda: date(2026, 7, 24))
    monkeypatch.setattr(
        m.tz_lib, "deployment_now",
        lambda: datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
    )

    assert m.main() == 0
    assert m.DASH.is_file() and m.SIDECAR.is_file()
    assert "| sources |" in m.DASH.read_text()
    assert '"wakeAgent": false' in capsys.readouterr().out

    # Seed a different prior distribution so the dashboard renders movement.
    m.SIDECAR.write_text('{"dist": {"sources": {"hot": 0, "warm": 1, "cold": 0}}}')
    assert m.main() == 0
    dash = m.DASH.read_text()
    assert "(+1)" in dash and "(-1)" in dash


def test_main_rejects_missing_wiki(tmp_path, monkeypatch, capsys):
    m = _load(tmp_path, monkeypatch)
    assert m._namespace_bases("sources") == [tmp_path / "wiki" / "sources"]
    assert m.main() == 1
    assert "wiki not found" in capsys.readouterr().err


def test_count_skips_missing_bases_and_index_pages(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    base = wiki / "sources"
    base.mkdir(parents=True)
    (wiki / "walkup").mkdir()
    (wiki / "walkup" / "schema.yaml").write_text("types: {}\n")
    for name in ("INDEX.md", "INDEX-p2.md", "_private.md"):
        (base / name).write_text("ignored")
    m = _load(tmp_path, monkeypatch)
    # The walk-up namespace does not exist, and reserved pages in the root are skipped.
    assert m._count_namespace("sources", {}, {"namespaces": {}}, date(2026, 1, 1)) == {
        "hot": 0, "warm": 0, "cold": 0,
    }


def test_main_recovers_from_malformed_prior_sidecar(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    m.OPDIR.mkdir(parents=True, exist_ok=True)
    m.SIDECAR.write_text("not-json")
    monkeypatch.setattr(m.tier_lib, "load_cfg", lambda _vault: {"namespaces": {}})
    assert m.main() == 0


def test_subdomain_without_namespace_config_is_skipped(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    (wiki / "entities").mkdir(parents=True)
    (wiki / "sub/entities").mkdir(parents=True)
    (wiki / "sub/schema.yaml").write_text("types: {}\n")
    (wiki / "sub/entities/a.md").write_text("---\ntype: entity\n---\n")
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(m.tier_lib, "load_cfg", lambda vault, namespace="": {"namespaces": {}})
    assert m._count_namespace(
        "entities", {}, {"namespaces": {"entities": {}}}, date(2026, 1, 1)
    ) == {"hot": 0, "warm": 0, "cold": 0}


def test_main_discovers_namespace_declared_only_by_subdomain(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    (wiki / "sub/widgets").mkdir(parents=True)
    (wiki / "sub/schema.yaml").write_text("types: {}\n")
    (wiki / "sub/widgets/a.md").write_text("---\ntype: widget\n---\n")
    m = _load(tmp_path, monkeypatch)
    root_cfg = {"namespaces": {}}
    sub_cfg = {"namespaces": {"widgets": {}}}
    monkeypatch.setattr(
        m.tier_lib, "load_cfg",
        lambda vault, namespace="": sub_cfg if namespace == "sub" else root_cfg,
    )
    monkeypatch.setattr(m.tz_lib, "deployment_today", lambda: date(2026, 1, 1))
    monkeypatch.setattr(
        m.tz_lib, "deployment_now", lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert m.main() == 0
    assert "| widgets |" in m.DASH.read_text()
