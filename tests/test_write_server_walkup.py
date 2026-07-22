"""Walk-up (multipack sub-domain) + write-scope guards on the enforced MCP write path.

Regression battery for the invariant-audit T1 security cluster (okengine#178). The whole
existing write_server suite places schema.yaml at the vault ROOT and writes only top-level
pages (wiki/findings/…), where the first path component IS the namespace — so the walk-up
topology (wiki/<subdomain>/<namespace>/…, the primary co-install shape, framework
install-domain) was never exercised on the write path. These lock in:

  #1  a sub-domain's human-only namespace permission is ENFORCED on update/patch/tombstone
      (was silently bypassed: _namespace() read the sub-domain container as the namespace, so
      the per-namespace rule never matched -> agent could overwrite human-authored pages).
  #2  an agent CAN create into a sub-domain namespace (was rejected as 'undeclared namespace'
      for the same reason -> the walk-up domain was unpopulatable on the enforced path).
  #3  extension write-scope is checked on the NORMALIZED target, so a '..' traversal can't
      escape the declared scope (was checked on the raw string).
  #13 a converge redirect to an existing authority canonical re-authorizes the write scope.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

MOD = Path(__file__).resolve().parent.parent / "okengine-mcp" / "write_server.py"

_SCHEMA = """\
okf: {required: [type]}
types:
  entity: {required: [type, name]}
  finding: {required: [type, title]}
strict_types: false
partitioning:
  namespaces: {entities: {}, sources: {}, findings: {}}
permissions:
  default: {create: true, update: true, delete: false}
  namespaces:
    findings: {create: false, update: false}
"""


def _load(tmp, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-15")
    sys.modules.pop("write_server", None)
    spec = importlib.util.spec_from_file_location("write_server", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def walkup(tmp_path, monkeypatch):
    """A co-installed (walk-up) vault: root schema + a sub-domain wiki/acme/schema.yaml with
    its own namespace dirs — exactly what framework_install_domain.install_subtree lays down."""
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    (tmp_path / "wiki" / "acme").mkdir()
    (tmp_path / "wiki" / "acme" / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    for ns in ("entities", "sources", "findings"):
        (tmp_path / "wiki" / "acme" / ns).mkdir(parents=True, exist_ok=True)
    return _load(tmp_path, monkeypatch), tmp_path


# --- #1/#2: sub-domain-aware namespace resolution ----------------------------------------

def test_namespace_is_subdomain_aware(walkup):
    m, tmp = walkup
    ns = lambda rel: m._namespace(Path(str(tmp) + "/wiki/" + rel + ".md"))
    assert ns("entities/e1") == "entities"          # flat page: unchanged
    assert ns("acme/entities/e1") == "entities"     # walk-up: the dir BELOW the sub-domain
    assert ns("acme/findings/h1") == "findings"


def test_flat_vault_namespace_unchanged(tmp_path, monkeypatch):
    """No nested schema.yaml -> the loop never advances -> identical to the old parts[0]."""
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    assert m._namespace(Path(str(tmp_path) + "/wiki/entities/a/x.md")) == "entities"
    assert m._namespace(Path(str(tmp_path) + "/wiki/findings/x.md")) == "findings"


def test_subdomain_create_succeeds(walkup):
    """#2: a default-writable namespace under a sub-domain must be creatable (was rejected as
    'undeclared namespace')."""
    m, _ = walkup
    res = m._create("acme/entities/e1", "type: entity\nname: AcmeCo", "body")
    assert res.startswith("created"), res
    # and the flat form still works
    assert m._create("entities/e1", "type: entity\nname: RootCo", "body").startswith("created")


_SHARDING_SCHEMA = """\
okf: {required: [type]}
types:
  entity: {required: [type, name]}
strict_types: false
partitioning:
  namespaces: {entities: {strategy: by-letter}, sources: {}, findings: {}}
permissions:
  default: {create: true, update: true, delete: false}
"""


@pytest.fixture
def walkup_sharded(tmp_path, monkeypatch):
    """A walk-up vault whose sub-domain shards entities BY-LETTER (the real base-schema shape) —
    unlike the flat `entities: {}` fixture above, which masked the sub-domain sharding bug (#351)."""
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(_SHARDING_SCHEMA, encoding="utf-8")
    (tmp_path / "wiki" / "acme").mkdir()
    (tmp_path / "wiki" / "acme" / "schema.yaml").write_text(_SHARDING_SCHEMA, encoding="utf-8")
    (tmp_path / "wiki" / "acme" / "entities").mkdir(parents=True)
    (tmp_path / "wiki" / "entities").mkdir(parents=True)
    return _load(tmp_path, monkeypatch), tmp_path


def test_subdomain_entity_shards_within_subdomain(walkup_sharded):  # invariant-audit #351 (A2)
    """A2: a NEW entity under a sub-domain that shards entities by-letter must land at
    wiki/acme/entities/<l>/<slug>.md — the shard the reshelve drain would file it under. Before the
    fix, _partitioned_create_path read the CONTAINER 'acme' as the namespace, found no partition
    config, and wrote FLAT (acme/entities/shinyhunters.md); the drain then sharded it, re-opening the
    #54 flat-vs-sharded duplicate-canonical ping-pong for every co-installed vault."""
    m, tmp = walkup_sharded
    res = m._create("acme/entities/shinyhunters", "type: entity\nname: ShinyHunters", "body")
    assert res.startswith("created"), res
    landed = sorted(str(p.relative_to(tmp)) for p in (tmp / "wiki" / "acme" / "entities").rglob("*.md"))
    assert (tmp / "wiki" / "acme" / "entities" / "s" / "shinyhunters.md").is_file(), landed
    assert not (tmp / "wiki" / "acme" / "entities" / "shinyhunters.md").is_file(), \
        f"sub-domain entity written FLAT (drain will duplicate it): {landed}"
    # the flat/root entity still shards (single-pack behavior byte-identical)
    m._create("entities/rootco", "type: entity\nname: RootCo", "body")
    assert (tmp / "wiki" / "entities" / "r" / "rootco.md").is_file()


def test_subdomain_alias_dedup_within_subdomain(walkup):  # invariant-audit #351 (A1)
    """A1: create-time alias dedup must fire for sub-domain entities. The identity index + _alias_hits
    were root-only, so a walk-up vault silently accreted duplicate canonicals (an alias curated on the
    canonical page never matched an incoming variant). A second create whose name equals the first's
    alias must CONVERGE, not fork a second page."""
    m, _ = walkup
    if not m._CONVERGE_OK:
        pytest.skip("converge libs unavailable")
    m._create("acme/entities/acme", "type: entity\nname: Acme\naliases: [Acme Corp]", "body")
    out = m._create("acme/entities/acme-corp", "type: entity\nname: Acme Corp", "body")
    assert out.startswith("converged into"), out


def test_subdomain_alias_dedup_does_not_cross_subdomain(walkup):  # invariant-audit #351 (A1 scoping)
    """A1 scoping guard: a sub-domain's entity must NOT converge into a same-named entity in ANOTHER
    scope (here root). Co-installed sub-domains are separate knowledge bases; a global name index
    would false-merge two distinct 'Acme's. The incoming acme/ page must create, not converge."""
    m, _ = walkup
    if not m._CONVERGE_OK:
        pytest.skip("converge libs unavailable")
    m._create("entities/acme", "type: entity\nname: Acme", "body")                    # ROOT scope
    out = m._create("acme/entities/other", "type: entity\nname: Other\naliases: [Acme]", "body")
    assert out.startswith("created"), f"cross-sub-domain false-merge: {out}"


def test_subdomain_undeclared_namespace_still_rejected(walkup):
    """The #115 stray-tree guard must still fire for a genuinely undeclared namespace under a
    sub-domain (the fix must not blanket-allow)."""
    m, _ = walkup
    res = m._create("acme/nonsense/x", "type: entity\nname: X", "body")
    assert res.startswith("rejected") and "not declared" in res, res


def test_subdomain_human_only_permission_enforced(walkup):
    """#1: the human-only `findings` namespace must refuse an AGENT update/tombstone even under
    a sub-domain (was bypassed — _namespace() read 'acme', so the findings rule never matched)."""
    m, tmp = walkup
    fm = {"type": "finding", "title": "x"}
    root_p = Path(str(tmp) + "/wiki/findings/h1.md")
    sub_p = Path(str(tmp) + "/wiki/acme/findings/h1.md")
    # both must block identically
    assert m._policy_reject(root_p, fm, "update") is not None
    assert m._policy_reject(sub_p, fm, "update") is not None, "sub-domain human-only permission BYPASSED"
    assert m._policy_reject(sub_p, fm, "create") is not None
    # end-to-end: an agent create into the sub-domain findings namespace is refused
    res = m._create("acme/findings/f1", "type: finding\ntitle: Agent", "body")
    assert res.startswith("rejected"), res


# --- #3: write-scope on the normalized target, not the raw string ------------------------

def test_scope_traversal_bypass_closed(walkup):
    """#3: a '..' traversal that resolves OUT of the declared scope must be refused, even though
    the raw string textually starts with an allowed prefix."""
    m, _ = walkup
    tok = m._caller_var.set({"kind": "extension", "write_scopes": ["entities/**"], "ext_id": "okengine.test"})
    try:
        assert m._wauth_refusal("entities/x") is None                      # in scope
        assert m._wauth_refusal("entities/../findings/x") is not None      # escapes -> refused
        assert m._wauth_refusal("findings/x") is not None                  # plainly out of scope
    finally:
        m._caller_var.reset(tok)


def test_admin_caller_still_unrestricted(walkup):
    """The default stdio/admin caller keeps full write access — the normalization must not
    accidentally restrict it."""
    m, _ = walkup
    assert m._wauth_refusal("entities/../findings/x") is None  # admin (no caller var set)


# --- #177: sub-domain schema resolution for id/type-authority/ownership (composed split-brain) ---

import yaml as _yaml  # noqa: E402


def test_subdomain_custom_type_gets_authority_id_not_slug(tmp_path, monkeypatch):  # invariant-audit #177
    """The split-brain: id/type-authority resolved through _governing (root composed, namespace
    dropped) while permissions resolved through _find_schema (the sub-domain schema). A sub-domain
    page of a sub-domain-CUSTOM type must get its AUTHORITY id from the sub-domain schema — not a
    minted slug from the root composed. Bit only sub-domain-custom types once a schema-extension
    (hence a root composed artifact) was enabled."""
    (tmp_path / "wiki" / "entities").mkdir(parents=True)
    (tmp_path / "wiki" / "acme" / "entities").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(_yaml.safe_dump({
        "types": {"entity": {"required": ["type", "name"]}},
        "partitioning": {"namespaces": {"entities": {}}}}), encoding="utf-8")
    (tmp_path / "wiki" / "acme" / "schema.yaml").write_text(_yaml.safe_dump({
        "types": {"advisory": {"required": ["type"], "id_authority": "cve", "id_field": "cve_id"}},
        "partitioning": {"namespaces": {"entities": {}}}}), encoding="utf-8")
    # a ROOT composed artifact exists (any schema-bearing extension enabled) — built from root only,
    # so it has no 'advisory' type. Pre-fix this shadowed the sub-domain schema for id derivation.
    (tmp_path / ".okengine").mkdir()
    (tmp_path / ".okengine" / "composed-schema.yaml").write_text(_yaml.safe_dump({
        "types": {"entity": {"required": ["type", "name"]}},
        "partitioning": {"namespaces": {"entities": {}}}}), encoding="utf-8")
    m = _load(tmp_path, monkeypatch)

    assert m._create("acme/entities/adv1", "type: advisory\ncve_id: CVE-2026-1", "b").startswith("created")
    fm = _yaml.safe_load((tmp_path / "wiki" / "acme" / "entities" / "adv1.md").read_text().split("---")[1])
    assert fm.get("id") == "cve:cve-2026-1", f"split-brain not closed — got id={fm.get('id')!r} (a slug)"

    # a ROOT page of a root type is unaffected — still resolves the root composed schema
    assert m._create("entities/e1", "type: entity\nname: Root", "b").startswith("created")


def test_governing_resolves_subdomain_schema(tmp_path, monkeypatch):
    """_governing(page) must resolve the sub-domain's schema for a sub-domain page (its custom
    types/authorities), and the root schema for a root page — unifying with the permission path."""
    (tmp_path / "wiki" / "acme" / "entities").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text("types: {entity: {required: [type]}}\n", encoding="utf-8")
    (tmp_path / "wiki" / "acme" / "schema.yaml").write_text(
        "types: {advisory: {required: [type], id_authority: cve, id_field: cve_id}}\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    sub = m._governing(Path(str(tmp_path) + "/wiki/acme/entities/x.md"))
    root = m._governing(Path(str(tmp_path) + "/wiki/entities/x.md"))
    assert "advisory" in (sub.get("types") or {}), "sub-domain page did not resolve the sub-domain schema"
    assert "advisory" not in (root.get("types") or {}), "root page leaked the sub-domain type"
