"""tier_refresh — tier-distribution dashboard. Regression for the walk-up sub-domain blindspot."""
import importlib.util
import sys
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
