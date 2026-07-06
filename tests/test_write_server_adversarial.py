"""Adversarial pass on the enforced write path (pre-release security review).

The write path (okengine-mcp/write_server.py) is THE security boundary: a
compromised, confused, or prompt-injected agent hits these guards. The tombstone
bug (okengine#166) proved the failure mode is *a guard an op forgets to call*, so
this battery throws every attack class at EVERY write op and asserts REFUSAL.

Attack classes: path traversal / vault escape, reserved-file overwrite, namespace
permission escape (create:false / update:false), schema-conformance bypass,
frontmatter field-loss. Any accepted write here is a boundary hole.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

MOD = Path(__file__).resolve().parent.parent / "okengine-mcp" / "write_server.py"

_SCHEMA = """\
okf:
  required: [type]
types:
  entity: {required: [type, name]}
  source: {required: [type, source_kind, publisher, published]}
strict_types: false
partitioning:
  namespaces:
    entities: {strategy: by-letter}
    sources: {strategy: by-date}
    register: {strategy: flat}
permissions:
  default: {create: true, update: true, delete: false}
  namespaces:
    register: {create: false, update: false}   # human-authored — agent read-only
protected_fields: [authority_id]
"""


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-15")
    sys.modules.pop("write_server", None)
    spec = importlib.util.spec_from_file_location("write_server", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = m
    spec.loader.exec_module(m)
    return m, tmp_path



def _created_path(root, res):
    """The actual (shard-normalized) file _create wrote — parse its 'created <rel>' return."""
    assert res.startswith("created"), res
    rel = res.split()[1]
    return root / "wiki" / rel


def _refused(res: str) -> bool:
    return isinstance(res, str) and res.split(":", 1)[0].strip().lower() in (
        "refused", "rejected", "error")


# ── 1. path traversal / vault escape ─────────────────────────────────────────
ESCAPE_PATHS = [
    "../../../etc/passwd",
    "../../.env",
    "../schema.yaml",
    "../pack.yaml",
    "../../.hermes-data/config.yaml",
    "/etc/passwd",
    "a/../../../../../../etc/hosts",
    "sources/../../../schema.yaml",
]


@pytest.mark.parametrize("bad", ESCAPE_PATHS)
def test_create_refuses_vault_escape(ws, bad, tmp_path):
    m, root = ws
    res = m._create(bad, "type: entity\nname: X", "pwned")
    assert _refused(res), f"create accepted escape path {bad!r}: {res}"
    # nothing landed outside wiki/
    assert not (root / "schema.yaml").read_text().startswith("pwned")
    assert not (root.parent / "etc").exists()


@pytest.mark.parametrize("op", ["_create", "_update", "_converge", "_append_section", "_patch"])
def test_all_write_ops_refuse_absolute_escape(ws, op):
    m, _ = ws
    fn = getattr(m, op)
    bad = "/etc/passwd"
    if op == "_patch":
        res = fn(bad, "x", "y")
    elif op == "_append_section":
        res = fn(bad, "H", "text")
    else:
        res = fn(bad, "type: entity\nname: X", "body")
    assert _refused(res), f"{op} accepted absolute path: {res}"


# ── 2. reserved-file overwrite ───────────────────────────────────────────────
RESERVED = ["INDEX.md", "log.md", "HOT.md", "_review-queue.md",
            "entities/a/INDEX.md", "index.md", ".hidden.md"]


@pytest.mark.parametrize("name", RESERVED)
def test_create_refuses_reserved_files(ws, name):
    m, _ = ws
    res = m._create(name.removesuffix(".md"), "type: entity\nname: X", "clobber")
    assert _refused(res), f"create accepted reserved file {name!r}: {res}"


def test_update_and_tombstone_refuse_reserved(ws):
    m, root = ws
    # seed a real log.md, then try to mutate it through the write ops
    (root / "wiki" / "log.md").write_text("- audit trail\n")
    assert _refused(m._update("log", "type: entity\nname: X", "clobber"))
    assert _refused(m._tombstone("log", "kill the audit trail"))
    assert "audit trail" in (root / "wiki" / "log.md").read_text()


# ── 3. namespace permission escape (create:false / update:false) ─────────────
def test_no_write_op_escapes_a_readonly_namespace(ws):
    """register/ is human-authored (create:false, update:false). EVERY mutating op
    must refuse it — this is the tombstone-class check across the whole surface."""
    m, root = ws
    # create refused
    assert _refused(m._create("register/x", "type: entity\nname: X", "b")), "create escaped"
    # seed a page directly (as a human would, via git) then try every mutation
    p = root / "wiki" / "register" / "seeded.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\ntype: entity\nname: Seeded\nversion: 1\n---\nbody\n")
    for op, call in [
        ("update", lambda: m._update("register/seeded", "type: entity\nname: X", "b")),
        ("tombstone", lambda: m._tombstone("register/seeded", "remove")),
        ("patch", lambda: m._patch("register/seeded", "body", "pwned")),
        ("append", lambda: m._append_section("register/seeded", "Notes", "injected")),
        ("converge", lambda: m._converge("register/seeded", "type: entity\nname: X")),
    ]:
        res = call()
        assert _refused(res), f"{op} escaped the read-only namespace: {res}"
    # the seeded page is byte-untouched
    assert "pwned" not in p.read_text() and "injected" not in p.read_text()
    assert "tombstoned" not in p.read_text()


# ── 4. schema-conformance bypass ─────────────────────────────────────────────
def test_no_op_writes_a_nonconformant_page(ws):
    """entity requires [type, name]; a page missing `name` must be refused by
    create AND must not be reachable by update/converge either."""
    m, root = ws
    assert _refused(m._create("entities/x/nonconf", "type: entity", "b")), "create wrote nonconformant"
    assert not (root / "wiki" / "entities" / "x" / "nonconf.md").exists()
    # a valid page, then an update that removes the required field via body-only? update MERGES
    # frontmatter so it can't drop name; prove the merge preserves it.
    r = m._create("entities/o/ok", "type: entity\nname: Real", "b")
    ok = _created_path(root, r)
    m._update(str(ok.relative_to(root / "wiki")).removesuffix(".md"), None, "new body")
    import yaml
    fm = yaml.safe_load(ok.read_text().split("---")[1])
    assert fm["name"] == "Real", "body-only update dropped a required field"


# ── 5. frontmatter field-loss ────────────────────────────────────────────────
def test_update_cannot_drop_an_existing_field(ws):
    """update merges — an agent cannot silently strip a curated/protected field by
    omitting it from the patch."""
    m, root = ws
    m._create("entities/a/acme", "type: entity\nname: Acme\nauthority_id: cve-1\naliases: [a, b]", "x")
    # patch that omits authority_id + aliases entirely
    res = m._update("entities/a/acme", "type: entity\nname: Acme Renamed", "x")
    import yaml
    fm = yaml.safe_load((root / "wiki" / "entities" / "a" / "acme.md").read_text().split("---")[1])
    assert fm.get("authority_id") == "cve-1", "update dropped a protected field"
    assert fm.get("aliases") == ["a", "b"], "update dropped a curated field"
    assert fm.get("name") == "Acme Renamed"          # the intended change did land


def test_patch_field_loss_guard_blocks_frontmatter_deletion(ws):
    """patch edits raw text — a patch that deletes a frontmatter line must be
    caught by the field-loss guard, not silently applied."""
    m, root = ws
    r = m._create("entities/b/beta", "type: entity\nname: Beta\nauthority_id: cve-9", "body")
    beta = _created_path(root, r)
    rel = str(beta.relative_to(root / "wiki")).removesuffix(".md")
    res = m._patch(rel, "authority_id: cve-9\n", "")   # try to patch OUT the field line
    assert _refused(res), f"patch silently deleted a frontmatter field: {res}"
    assert "cve-9" in beta.read_text()


# ── 6. traversal that RESOLVES onto a reserved file inside wiki/ ──────────────
def test_traversal_onto_reserved_is_refused(ws):
    """`foo/../log` collapses to `log` inside wiki/ — the escape guard misses it
    (still inside wiki/), so _reserved_refuse must catch it after normalization."""
    m, root = ws
    (root / "wiki" / "log.md").write_text("- audit\n")
    for bad in ("foo/../log", "entities/a/../../INDEX", "x/../HOT"):
        res = m._create(bad, "type: entity\nname: X", "clobber")
        assert _refused(res), f"traversal-to-reserved {bad!r} accepted: {res}"
    assert "audit" in (root / "wiki" / "log.md").read_text()


# ── 7. tombstoned-id resurrection (converge) ─────────────────────────────────
def test_converge_refuses_to_resurrect_a_tombstoned_id(ws):
    """A tombstoned id must never be re-created — converge upserts by id and must
    refuse an id the registry knows is tombstoned."""
    m, root = ws
    r = m._create("entities/a/acme", "type: entity\nname: Acme\nid: authority:acme", "b")
    acme = _created_path(root, r)
    rel = str(acme.relative_to(root / "wiki")).removesuffix(".md")
    m._tombstone(rel, "merged away")
    res = m._converge(rel, "type: entity\nname: Acme Reborn\nid: authority:acme")
    # either the converge refuses, or (if the id didn't register) it must not
    # un-tombstone the page
    txt = acme.read_text()
    assert _refused(res) or "tombstoned" in txt, f"tombstoned id resurrected: {res}"


# ── 8. flag_for_review cannot write an arbitrary target ──────────────────────
def test_flag_only_touches_the_review_queue(ws):
    """flag_for_review records a path in a note but must only ever write the review
    queue — never the (attacker-controlled) target path."""
    m, root = ws
    before = set(p.name for p in (root / "wiki").rglob("*.md"))
    m._flag("entities/a/does-not-exist", "note")
    after = set(p.name for p in (root / "wiki").rglob("*.md"))
    new = after - before
    assert new <= {"_review-queue.md", "log.md"}, f"flag wrote unexpected files: {new}"  # log.md = audit trail
    assert not (root / "wiki" / "entities" / "a" / "does-not-exist.md").exists()
