"""The reshelve drain must discover namespaces through the same schema precedence the mover
uses (okengine#519).

okengine#515 fixed `okf_migrate._governing_schema` to prefer the composed artifact over the
pack's raw `schema.yaml`, because a composed pack declares partitioning the raw file omits.
But `reshelve.py` — the drain that is the mover's only scheduled caller — kept its OWN reader
of `root/schema.yaml` to decide WHICH namespaces to iterate. So the mover could resolve
`sources: by-date` correctly and never be asked about `sources` at all: on the live vault the
raw schema declared four namespaces, `sources` was not among them, and the drain ran to
completion, printed the namespaces it did process, and exited 0 while 989 misfiled source
pages sat untouched.

That is the failure mode worth a permanent test: not a crash, not a wrong answer, but a
silent omission that is indistinguishable from "nothing to do". The fix makes okf_migrate the
sole owner of the precedence rule, so the two cannot drift again.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
MIGRATE = REPO / "scripts" / "cron" / "okf_migrate.py"
RESHELVE = REPO / "scripts" / "cron" / "reshelve.py"


def _load(name: str, path: Path):
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _composed_pack(root: Path):
    """The live shape: the raw schema omits `sources`, the composition declares it."""
    (root / "wiki" / "sources").mkdir(parents=True)
    (root / "wiki" / "entities").mkdir(parents=True)
    (root / ".okengine").mkdir()
    (root / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n"
        "    entities: {strategy: by-letter}\n"
        "    concepts: {strategy: by-letter}\n"
        "    categories: {strategy: flat}\n"
        "    questions: {strategy: flat}\n", encoding="utf-8")
    (root / ".okengine" / "composed-schema.yaml").write_text(
        "partitioning:\n  namespaces:\n"
        "    entities: {strategy: by-letter}\n"
        "    concepts: {strategy: by-letter}\n"
        "    sources: {strategy: by-date, date_field: published}\n"
        "    categories: {strategy: flat}\n"
        "    questions: {strategy: flat}\n", encoding="utf-8")


def test_a_namespace_declared_only_in_the_composition_is_discovered(tmp_path):
    """The bug: reshelve read the raw schema, so `sources` was never even offered to the
    mover — and the drain still exited 0."""
    m = _load("okf_migrate", MIGRATE)
    m._SCHEMA_CACHE.clear()
    _composed_pack(tmp_path)
    assert "sources" in m.partitioned_namespaces(tmp_path), (
        "the composed schema is what the write path enforces; a namespace it partitions must "
        "be reshelved, or pages accumulate off-shard with the drain reporting success")


def test_flat_namespaces_are_not_reshelved(tmp_path):
    m = _load("okf_migrate", MIGRATE)
    m._SCHEMA_CACHE.clear()
    _composed_pack(tmp_path)
    got = m.partitioned_namespaces(tmp_path)
    assert "categories" not in got and "questions" not in got


def test_a_namespace_with_a_null_config_is_treated_as_flat(tmp_path):
    """Live composed schemas carry `gaps:`/`frontier:` with no value; `None.get` would raise
    and take the whole drain down."""
    m = _load("okf_migrate", MIGRATE)
    m._SCHEMA_CACHE.clear()
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    gaps:\n    entities: {strategy: by-letter}\n",
        encoding="utf-8")
    assert m.partitioned_namespaces(tmp_path) == ["entities"]


def test_subdomain_namespaces_are_prefixed_and_not_duplicated(tmp_path):
    m = _load("okf_migrate", MIGRATE)
    m._SCHEMA_CACHE.clear()
    _composed_pack(tmp_path)
    sub = tmp_path / "wiki" / "sub"
    sub.mkdir()
    (sub / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    sources: {strategy: by-date}\n", encoding="utf-8")
    got = m.partitioned_namespaces(tmp_path)
    assert "sub/sources" in got, "a sub-domain's namespace is addressed by its prefixed key"
    assert got.count("sources") == 1, "the root namespace must not be emitted twice"


def test_a_vault_with_no_schema_yields_nothing_rather_than_raising(tmp_path):
    m = _load("okf_migrate", MIGRATE)
    m._SCHEMA_CACHE.clear()
    (tmp_path / "wiki").mkdir()
    assert m.partitioned_namespaces(tmp_path) == []


def test_reshelve_asks_okf_migrate_and_moves_every_namespace_it_names(tmp_path, monkeypatch):
    """End-to-end on the drain itself: whatever okf_migrate reports, reshelve must pass each
    one to the mover. Guards against the discovery being fixed while the loop drops entries."""
    m = _load("okf_migrate", MIGRATE)
    m._SCHEMA_CACHE.clear()
    _composed_pack(tmp_path)
    r = _load("reshelve", RESHELVE)

    called: list[str] = []
    monkeypatch.setattr(r.okf_migrate, "main", lambda argv: called.append(argv[1]) or 0)
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    assert r.main() == 0
    assert "sources" in called, f"the drain skipped sources entirely; ran {called}"
    assert set(called) == set(r.okf_migrate.partitioned_namespaces(tmp_path))
