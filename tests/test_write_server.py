"""Regression: the G1 enforced MCP write path.

Verifies write_server's plain logic helpers validate against the walk-up
schema.yaml BEFORE writing, append to wiki/log.md on success, dedupe-refuse on
re-create, version-bump on update, retain-and-mark on tombstone, and queue
flags without mutating the target.

Tests call the plain `_create`/`_update`/`_tombstone`/`_flag` helpers directly
(the @mcp.tool() wrappers merely delegate, and `mcp` may be absent in the host
test env). The module is loaded by file path so no package install is needed.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "okengine-mcp" / "write_server.py"

_SCHEMA = """\
okf:
  required: [type]
types:
  source:
    required: [type, source_kind, publisher, published]
  entity:
    required: [type, name]
strict_types: false
permissions:
  default: {create: true, update: true, delete: false}
  namespaces:
    findings:
      create: false
      update: false
review:
  confidence_field: confidence
  confidence_review_values: [confirmed, false-positive, refuted]
  review_on_change_fields: [verified_by]
"""


def _load():
    spec = importlib.util.spec_from_file_location("write_server", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A temp vault with wiki/ and a governing schema.yaml so the validator fires.

    schema.yaml sits at the vault root (above wiki/), so the walk-up validator
    finds it for any wiki/**.md page. Date is pinned for deterministic logs.
    """
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-15")
    # Fresh import so module-level VAULT/WIKI pick up the env (helpers also
    # re-read WIKI_PATH at call time, but be explicit).
    sys.modules.pop("write_server", None)
    m = _load()
    return m, tmp_path


def _log_text(root: Path) -> str:
    log = root / "wiki" / "log.md"
    return log.read_text(encoding="utf-8") if log.exists() else ""


_DRIFT_SCHEMA = """\
okf:
  required: [type]
types:
  entity:
    required: [type, name]
strict_types: false
permissions:
  default: {create: true, update: true, delete: false}
field_aliases:
  country: suspected_origin
value_aliases:
  suspected_origin: {CN: China, US: United States}
  status: {active: live}
allowed:
  intrusion-set: [suspected_origin, motivation]
"""


@pytest.fixture
def drift_vault(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(_DRIFT_SCHEMA, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-15")
    sys.modules.pop("write_server", None)
    return _load(), tmp_path


def test_drift_field_and_value_aliases_normalized(drift_vault):
    """okengine#46: agent drift converges on the schema vocab BEFORE write —
    country->suspected_origin (key), CN->China + active->live (values)."""
    import yaml
    m, root = drift_vault
    res = m._create("entities/a/apt-x",
                    "type: intrusion-set\nname: APT-X\ncountry: CN\nstatus: active\nmotivation: espionage",
                    "body")
    assert res.startswith("created"), res
    fm = yaml.safe_load((root / "wiki" / "entities" / "a" / "apt-x.md").read_text().split("---")[1])
    assert fm.get("suspected_origin") == "China" and "country" not in fm
    assert fm.get("status") == "live"
    assert "unknown" not in res.lower()                  # every field known -> not flagged


def test_drift_unknown_field_flagged_not_rejected(drift_vault):
    """okengine#46: a field outside the type's allowed set is flagged for review (G3), kept, not
    rejected or dropped."""
    import yaml
    m, root = drift_vault
    res = m._create("entities/a/apt-y",
                    "type: intrusion-set\nname: APT-Y\nbogus_field: x\nmotivation: espionage", "body")
    assert res.startswith("created"), res                # flagged, NOT rejected
    txt = (root / "wiki" / "entities" / "a" / "apt-y.md").read_text()
    assert "needs_review: true" in txt
    fm = yaml.safe_load(txt.split("---")[1])
    assert fm.get("bogus_field") == "x"                  # preserved, just surfaced


def test_valid_create(vault):
    m, root = vault
    res = m._create(
        "entities/vendor/acme",
        "type: entity\nname: Acme Corp",
        "Acme is a vendor.",
    )
    assert res.startswith("created"), res
    p = root / "wiki" / "entities" / "vendor" / "acme.md"
    assert p.is_file()
    import yaml
    fm = yaml.safe_load(p.read_text().split("---")[1])
    assert fm["version"] == 1
    assert fm["last_updated"] == "2026-06-15"
    assert "create" in _log_text(root)
    assert "entities/vendor/acme.md v1" in _log_text(root)


def test_entity_shard_normalized_to_one_level(vault):
    """okengine#48: a two-level entity shard collapses to the one-level canonical path, so the
    agent can't create a stale duplicate of entities/<c>/<slug>.md. Non-shard segments and
    other namespaces are untouched."""
    m, root = vault
    assert m._normalize_entity_shard("entities/c/v/cve-2021-44228.md") == "entities/c/cve-2021-44228.md"
    assert m._normalize_entity_shard("entities/c/cve-2021-44228.md") == "entities/c/cve-2021-44228.md"
    assert m._normalize_entity_shard("entities/vendor/acme.md") == "entities/vendor/acme.md"   # multi-char kept
    assert m._normalize_entity_shard("sources/2026/06/x.md") == "sources/2026/06/x.md"          # other ns kept
    # end-to-end: a create at a two-level shard lands at the one-level path
    res = m._create("entities/a/p/apt-test", "type: entity\nname: APT-Test", "body")
    assert res.startswith("created"), res
    assert (root / "wiki" / "entities" / "a" / "apt-test.md").is_file()
    assert not (root / "wiki" / "entities" / "a" / "p" / "apt-test.md").exists()


def test_entity_shard_preserves_resharded_two_level(vault):
    """okengine invariant-audit: once a hot first-letter leaf is resharded to two levels
    (entities/<l>/<2nd>/<slug>.md), the enforced write path must NOT collapse the resharded canonical
    back to one level — that refuses/duplicates writes on a mature vault. Shard letters recomputed
    from the slug either way, so an arbitrary/old path still resolves to the real canonical."""
    m, root = vault
    resh = root / "wiki" / "entities" / "c" / "v"
    resh.mkdir(parents=True)
    (resh / "cve-2021-44228.md").write_text("---\ntype: entity\nname: x\n---\nbody\n")
    # the resharded canonical is preserved (NOT collapsed to the nonexistent one-level path)
    assert m._normalize_entity_shard("entities/c/v/cve-2021-44228.md") == "entities/c/v/cve-2021-44228.md"
    # an agent passing the OLD one-level path is redirected UP to the resharded canonical
    assert m._normalize_entity_shard("entities/c/cve-2021-44228.md") == "entities/c/v/cve-2021-44228.md"
    # an arbitrary wrong second-shard is corrected to the real resharded location
    assert m._normalize_entity_shard("entities/c/z/cve-2021-44228.md") == "entities/c/v/cve-2021-44228.md"
    # a DIFFERENT slug in the same resharded leaf lands two-level (leaf has been resharded)
    assert m._normalize_entity_shard("entities/c/cobalt-strike.md") == "entities/c/o/cobalt-strike.md"


def test_malformed_create_rejected(vault):
    m, root = vault
    # type: source but missing publisher/published required fields.
    res = m._create("sources/bad", "type: source\nsource_kind: blog", "body")
    assert res.startswith("rejected:"), res
    assert "publisher" in res and "published" in res
    p = root / "wiki" / "sources" / "bad.md"
    assert not p.exists(), "rejected create must write nothing"
    assert _log_text(root) == "", "rejected create must not append a log line"


def test_path_escape_prefix_sibling_refused(vault):
    """A sibling like <vault>/wiki_evil must not pass a string-prefix check for
    <vault>/wiki. This locks the guard to Path.relative_to semantics."""
    m, root = vault
    res = m._create("../wiki_evil/pwn", "type: entity\nname: Pwn", "body")
    assert res.startswith("refused:"), res
    assert not (root / "wiki_evil" / "pwn.md").exists()


def test_flag_path_escape_refused(vault):
    m, root = vault
    res = m._flag("../wiki_evil/pwn", "bad\nnote")
    assert res.startswith("refused:"), res
    assert not (root / "wiki" / "_review-queue.md").exists()


def test_dedupe_refuse(vault):
    m, root = vault
    m._create("entities/vendor/acme", "type: entity\nname: Acme", "v1 body")
    p = root / "wiki" / "entities" / "vendor" / "acme.md"
    before = p.read_text()
    res = m._create("entities/vendor/acme", "type: entity\nname: Acme2", "v2 body")
    assert res.startswith("refused:") and "already exists" in res
    assert p.read_text() == before, "dedupe-refuse must leave the file unchanged"


def test_update_bumps_version(vault):
    m, root = vault
    m._create("entities/vendor/acme", "type: entity\nname: Acme", "v1 body")
    res = m._update("entities/vendor/acme", "name: Acme Corp", "v2 body")
    assert res.startswith("updated") and "v2" in res, res
    p = root / "wiki" / "entities" / "vendor" / "acme.md"
    import yaml
    fm = yaml.safe_load(p.read_text().split("---")[1])
    assert fm["version"] == 2
    assert fm["name"] == "Acme Corp"
    assert fm["last_updated"] == "2026-06-15"
    assert "v2 body" in p.read_text()
    assert "update" in _log_text(root)


def test_update_invalid_rejected_original_untouched(vault):
    m, root = vault
    m._create(
        "sources/acme-blog",
        "type: source\nsource_kind: blog\npublisher: Acme\npublished: 2026-01-01",
        "body",
    )
    p = root / "wiki" / "sources" / "acme-blog.md"
    before = p.read_text()
    # Strip a required field -> must be rejected, original (v1) preserved.
    res = m._update("sources/acme-blog", "publisher: null", None)
    assert res.startswith("rejected:"), res
    assert p.read_text() == before, "rejected update must not alter the file"
    import yaml
    fm = yaml.safe_load(p.read_text().split("---")[1])
    assert fm["version"] == 1


def test_update_malformed_existing_frontmatter_refused(vault):
    m, root = vault
    p = root / "wiki" / "entities" / "bad.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\ntype: entity\nname: [unterminated\n---\nbody")
    before = p.read_text()

    res = m._update("entities/bad", "type: entity\nname: Fixed", "new body")
    assert res.startswith("rejected:"), res
    assert "invalid frontmatter" in res
    assert p.read_text() == before


def test_tombstone_retains_file(vault):
    m, root = vault
    m._create("entities/vendor/acme", "type: entity\nname: Acme", "body")
    res = m._tombstone("entities/vendor/acme", "merged into acme-inc",
                       superseded_by="entities/vendor/acme-inc")
    assert res.startswith("tombstoned"), res
    p = root / "wiki" / "entities" / "vendor" / "acme.md"
    assert p.is_file(), "tombstone must NOT delete the file"
    import yaml
    fm = yaml.safe_load(p.read_text().split("---")[1])
    assert fm["status"] == "tombstoned"
    assert fm["tombstone_reason"] == "merged into acme-inc"
    assert fm["superseded_by"] == "entities/vendor/acme-inc"
    assert fm["version"] == 2
    assert "tombstone" in _log_text(root)


def test_flag_for_review(vault):
    m, root = vault
    m._create("entities/vendor/acme", "type: entity\nname: Acme", "body")
    p = root / "wiki" / "entities" / "vendor" / "acme.md"
    before = p.read_text()
    res = m._flag("entities/vendor/acme", "publisher attribution looks wrong")
    assert res.startswith("flagged"), res
    queue = root / "wiki" / "_review-queue.md"
    assert queue.is_file()
    qtext = queue.read_text()
    assert "publisher attribution looks wrong" in qtext
    assert "entities/vendor/acme" in qtext
    assert p.read_text() == before, "flag must not mutate the target page"
    assert "flag" in _log_text(root)


# --- G2 structural permissions + G3 review FLAGS (not gates) --------------

def test_tombstone_respects_update_denied_namespace(vault):
    """G2 via tombstone (okengine#166): a tombstone IS an update — a namespace the
    agent may not update (e.g. a human-authored or federated read-only lookup tree)
    must reject it through the same permission matrix as every other mutation.
    This path bypassed _policy_reject before the fix."""
    m, root = vault
    p = root / "wiki" / "findings" / "seeded.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: entity\nname: Seeded\nversion: 1\n---\nbody\n")
    res = m._tombstone("findings/seeded", "should not land")
    assert res.startswith("rejected:") and "not agent-writable" in res, res
    assert "tombstoned" not in p.read_text(), "file must be untouched"


def test_create_denied_namespace_refused(vault):
    """G2: a namespace with create:false is a STRUCTURAL boundary (human-authored)
    — the one hard reject. (delete:false is exercised via tombstone above.)"""
    m, root = vault
    res = m._create("findings/intrusion-x", "type: entity\nname: X", "body")
    assert res.startswith("rejected:") and "not agent-writable" in res, res
    assert not (root / "wiki" / "findings" / "intrusion-x.md").exists()


def _fm(p):
    import yaml
    return yaml.safe_load(p.read_text().split("---")[1])


def test_confidence_verdict_flags_not_blocks_on_create(vault):
    """G3: an agent CAN assert a categorical verdict — the write LANDS, but the
    page is flagged (needs_review + review queue), never blocked."""
    m, root = vault
    res = m._create("entities/vendor/acme",
                    "type: entity\nname: Acme\nconfidence: confirmed", "body")
    assert res.startswith("created") and "flagged for review" in res, res
    p = root / "wiki" / "entities" / "vendor" / "acme.md"
    assert p.is_file()
    assert _fm(p)["needs_review"] is True
    q = (root / "wiki" / "_review-queue.md").read_text()
    assert "entities/vendor/acme.md" in q and "confidence" in q
    assert "review-flag" in _log_text(root)


def test_numeric_and_categorical_levels_do_not_flag(vault):
    """G3: numeric scores + low/medium/high are free — no flag, no needs_review."""
    m, root = vault
    assert m._create("entities/vendor/num", "type: entity\nname: N\nconfidence: 0.7", "b") \
        .startswith("created")
    res = m._create("entities/vendor/med", "type: entity\nname: M\nconfidence: medium", "b")
    assert res.startswith("created") and "flagged" not in res, res
    assert "needs_review" not in _fm(root / "wiki" / "entities" / "vendor" / "med.md")


def test_escalation_flags_but_preserve_does_not_refire(vault):
    """G3: escalating TO confirmed on update flags (write lands); a later update
    that PRESERVES the existing confirmed value does not re-flag."""
    m, root = vault
    m._create("entities/vendor/esc", "type: entity\nname: E\nconfidence: 0.6", "body")
    p = root / "wiki" / "entities" / "vendor" / "esc.md"
    res = m._update("entities/vendor/esc", "confidence: confirmed", None)
    assert res.startswith("updated") and "flagged for review" in res, res
    assert _fm(p)["confidence"] == "confirmed" and _fm(p)["needs_review"] is True
    q_lines_before = (root / "wiki" / "_review-queue.md").read_text().count("esc.md")
    # preserve confirmed (only touch name) -> no new flag line
    res = m._update("entities/vendor/esc", "name: E2", None)
    assert res.startswith("updated") and "flagged" not in res, res
    q_lines_after = (root / "wiki" / "_review-queue.md").read_text().count("esc.md")
    assert q_lines_after == q_lines_before, "preserve must not re-flag"


def test_review_on_change_field_flags(vault):
    """G3: setting a configured review_on_change_field flags the page (not blocks)."""
    m, root = vault
    m._create("entities/vendor/acme", "type: entity\nname: Acme", "body")
    res = m._update("entities/vendor/acme", "verified_by: analyst", None)
    assert res.startswith("updated") and "flagged for review" in res, res
    p = root / "wiki" / "entities" / "vendor" / "acme.md"
    assert _fm(p)["needs_review"] is True and _fm(p)["verified_by"] == "analyst"


# --- G1.1 surgical edits: patch_entity + append_to_section ----------------

def _mk(m, path, fm, body):
    assert m._create(path, fm, body).startswith("created")
    return None


def test_patch_replaces_one_place_body_preserved(vault):
    m, root = vault
    _mk(m, "concepts/c/ransomware", "type: concept\nname: R\nsources: [x]",
        "Intro.\n\nSee [[concepts/old-link]] for more.\n\nOutro stays.")
    res = m._patch("concepts/c/ransomware",
                   "[[concepts/old-link]]", "[[concepts/r/ransomware-ops]]")
    assert res.startswith("patched") and "v2" in res, res
    p = root / "wiki" / "concepts" / "c" / "ransomware.md"
    txt = p.read_text()
    assert "[[concepts/r/ransomware-ops]]" in txt
    assert "[[concepts/old-link]]" not in txt
    assert "Intro." in txt and "Outro stays." in txt   # body preserved
    assert _fm(p)["name"] == "R" and _fm(p)["version"] == 2
    assert "patch" in _log_text(root)


def test_patch_rejects_not_found_and_ambiguous(vault):
    m, root = vault
    _mk(m, "entities/vendor/acme", "type: entity\nname: Acme", "foo bar foo")
    p = root / "wiki" / "entities" / "vendor" / "acme.md"
    before = p.read_text()
    assert m._patch("entities/vendor/acme", "nope", "x").startswith("rejected:")
    assert p.read_text() == before
    r = m._patch("entities/vendor/acme", "foo", "baz")  # 2 matches
    assert r.startswith("rejected:") and "matches 2" in r
    assert p.read_text() == before


def test_patch_field_loss_guard(vault):
    """An edit that drops an existing frontmatter key is rejected."""
    m, root = vault
    _mk(m, "entities/vendor/acme",
        "type: entity\nname: Acme\npriority_tier: direct", "body")
    p = root / "wiki" / "entities" / "vendor" / "acme.md"
    before = p.read_text()
    # try to patch out the priority_tier line entirely
    res = m._patch("entities/vendor/acme", "priority_tier: direct\n", "")
    assert res.startswith("rejected:") and "priority_tier" in res, res
    assert p.read_text() == before, "field-loss reject must leave the file untouched"


def test_patch_insert_section_before_heading(vault):
    m, root = vault
    _mk(m, "predictions/p1",
        "type: prediction\nstatus: open\nconfidence: 0.5\nsubject: X\nresolves_by: 2027-01-01",
        "## Reasoning\n\nBecause.\n\n## What would refute this\n\nIf not.\n")
    anchor = "## What would refute this"
    new = "## Structural analysis\n\nPaths.\n\n" + anchor
    res = m._patch("predictions/p1", anchor, new)
    assert res.startswith("patched"), res
    txt = (root / "wiki" / "predictions" / "p1.md").read_text()
    assert txt.index("## Structural analysis") < txt.index("## What would refute this")
    assert "## Reasoning" in txt


def test_append_to_existing_section(vault):
    m, root = vault
    _mk(m, "predictions/p2",
        "type: prediction\nstatus: open\nconfidence: 0.5\nsubject: X\nresolves_by: 2027-01-01",
        "## Claim\n\nC.\n\n## Evidence log\n\n- old entry\n")
    res = m._append_section("predictions/p2", "Evidence log", "- new entry 2026-06-15")
    assert res.startswith("appended") and "existing section" in res, res
    txt = (root / "wiki" / "predictions" / "p2.md").read_text()
    assert "- old entry" in txt and "- new entry 2026-06-15" in txt
    # new entry sits inside the Evidence log section (after old entry)
    assert txt.index("- old entry") < txt.index("- new entry")
    assert _fm(root / "wiki" / "predictions" / "p2.md")["version"] == 2


def test_append_creates_missing_section(vault):
    m, root = vault
    _mk(m, "predictions/p3",
        "type: prediction\nstatus: open\nconfidence: 0.5\nsubject: X\nresolves_by: 2027-01-01",
        "## Claim\n\nC.\n")
    res = m._append_section("predictions/p3", "Postmortem", "Resolved confirmed.")
    assert res.startswith("appended") and "section created" in res, res
    txt = (root / "wiki" / "predictions" / "p3.md").read_text()
    assert "## Postmortem" in txt and "Resolved confirmed." in txt
    assert "## Claim" in txt


def test_reserved_files_refused(vault):
    """Engine-managed structural files (log.md/index.md/INDEX.md/AGENTS.md/HOT.md/
    _review-queue.md) are NOT agent-writable — append/patch/update/create on them
    must refuse, never inject frontmatter into a plain changelog."""
    m, root = vault
    log = root / "wiki" / "log.md"
    log.write_text("# Wiki Log\n\n- existing entry\n")
    before = log.read_text()
    assert m._append_section("log", "Whatever", "- new").startswith("refused:")
    assert m._patch("log", "existing", "changed").startswith("refused:")
    assert m._update("log", "version: 9", None).startswith("refused:")
    assert m._create("index", "type: dashboard\ntitle: X", "b").startswith("refused:")
    assert m._create("_review-queue", "type: x", "b").startswith("refused:")
    assert log.read_text() == before, "reserved file must be untouched (no frontmatter injected)"
    assert not log.read_text().startswith("---")


def test_leading_wiki_prefix_not_doubled(vault):
    """A path that already carries a leading wiki/ must land at wiki/<rel>, not
    wiki/wiki/<rel>. The doubled path stays *inside* wiki/ so the escape guard
    never catches it — it silently misfiles every page and breaks raw-drain
    dedup (okengine#31)."""
    m, root = vault
    res = m._create("wiki/entities/vendor/acme", "type: entity\nname: Acme", "body")
    assert res.startswith("created"), res
    good = root / "wiki" / "entities" / "vendor" / "acme.md"
    doubled = root / "wiki" / "wiki" / "entities" / "vendor" / "acme.md"
    assert good.is_file(), "page must land at wiki/entities/..."
    assert not doubled.exists(), "must NOT double into wiki/wiki/..."
    # The plain and wiki/-prefixed forms normalize to the same resolved path.
    assert m._safe("entities/vendor/acme") == m._safe("wiki/entities/vendor/acme")


def test_over_qualified_vault_path_not_shadowed(vault):
    """Over-qualified-path variant of okengine#31/#34: an agent that follows the persona's 'prefer the absolute form'
    guidance (correct for file_read) may pass a write tool the FULL vault path —
    `<vault>/wiki/sources/...` or the vault-relative `<...>/wiki/sources/...`.
    Those stay *inside* wiki/, so the escape guard misses them and the write lands
    in a shadow `wiki/<vault>/wiki/...` tree — a duplicate canonical. _safe must
    collapse the over-qualified prefix to the wiki-relative tail."""
    m, root = vault
    rel = "sources/2026/06/09/unit42-cloud-logging"
    body = "type: source\nsource_kind: vendor-research\npublisher: Unit 42\npublished: 2026-06-09"
    # 1) absolute vault/wiki form (the exact shape seen in production)
    abs_form = str(root / "wiki" / rel)
    res = m._create(abs_form, body, "x")
    assert res.startswith("created"), res
    good = root / "wiki" / "sources" / "2026" / "06" / "09" / "unit42-cloud-logging.md"
    assert good.is_file(), "page must land at the canonical wiki/sources/... path"
    # no shadow tree anywhere under wiki/ (e.g. wiki/.../wiki/sources/...)
    shadows = [p for p in (root / "wiki").rglob("unit42-cloud-logging.md")
               if p != good]
    assert not shadows, f"over-qualified path created a shadow page: {shadows}"
    # 2) absolute & vault-relative over-qualified forms normalize to the canonical path
    assert m._safe(abs_form) == m._safe(rel)
    assert m._safe(abs_form.lstrip("/")) == m._safe(rel)


def test_create_slug_variant_does_not_duplicate(vault):
    """okengine#98/#99/#100: create_entity keys on IDENTITY, not the filename. The
    same entity written a second time under a cosmetically different path/slug (here
    `Akira` vs `akira`, different shard dir) derives the same minted slug id and is
    refused as a slug collision + flagged — so exactly ONE canonical exists instead
    of a stale duplicate the assembler never reconciles."""
    m, root = vault
    r1 = m._create("entities/a/akira", "type: entity\nname: Akira", "first")
    assert r1.startswith("created"), r1
    # same identity (name), cosmetically different filename + shard dir
    r2 = m._create("entities/vendor/Akira", "type: entity\nname: akira", "second")
    assert r2.startswith("refused:") and "slug id" in r2 and "akira" in r2, r2
    # only the first canonical exists; the duplicate path was never written
    assert (root / "wiki" / "entities" / "a" / "akira.md").is_file()
    assert not (root / "wiki" / "entities" / "vendor" / "Akira.md").exists()
    assert "slug id collision" in (root / "wiki" / "_review-queue.md").read_text()
    # the stamped id is the content-derived identity, independent of the path
    assert _fm(root / "wiki" / "entities" / "a" / "akira.md")["id"] == "entities:akira"


def test_create_stamps_name_from_h1_when_absent(vault):
    """Source ingest (agent -> okengine-write) often puts the article title in the
    body's `# H1` but sets no `name`/`title`, leaving the page nameless. The write
    path derives `name` from the true H1 when both are absent — never overriding a
    curated name, and never picking up a `## Summary` section heading."""
    m, root = vault
    body = "## Summary\nblah\n\n# Crypto Clipper uses Tor for propagation\nbody\n"
    res = m._create("sources/2026/06/crypto-clipper", "type: source\npublisher: MS\n"
                    "source_kind: vendor-research\npublished: 2026-06-17", body)
    assert res.startswith("created"), res
    p = root / "wiki" / "sources" / "2026" / "06" / "crypto-clipper.md"
    assert _fm(p)["name"] == "Crypto Clipper uses Tor for propagation"   # from the # H1, not ## Summary

    # a curated name is never overridden
    m._create("entities/a/acme", "type: entity\nname: Acme Corp", "# Something Else\nx")
    assert _fm(root / "wiki" / "entities" / "a" / "acme.md")["name"] == "Acme Corp"

    # no name and no true H1 -> left nameless (no spurious stamp; source has no
    # required `name`, so it still creates)
    m._create("sources/2026/06/nohdr", "type: source\npublisher: MS\n"
              "source_kind: vendor-research\npublished: 2026-06-17", "## Summary only\nno h1 here\n")
    assert "name" not in _fm(root / "wiki" / "sources" / "2026" / "06" / "nohdr.md")


def test_blank_wiki_path_falls_back_to_default(monkeypatch):
    """A set-but-blank WIKI_PATH must fall back to the /opt/vault default, not
    resolve to a *relative* wiki/ under CWD (okengine#34)."""
    monkeypatch.setenv("WIKI_PATH", "")
    sys.modules.pop("write_server", None)
    m = _load()
    assert m._wiki().is_absolute(), "blank WIKI_PATH must not yield a relative path"
    assert m._wiki() == Path("/opt/vault") / "wiki"


def test_create_stamps_immutable_created(vault, monkeypatch):
    """A create stamps an immutable `created` (ingest date); a later update preserves it while
    last_updated moves — so age/recent-ingest reporting is accurate (not last_updated drift)."""
    import yaml
    m, root = vault
    m._create("entities/f/foo", "type: entity\nname: Foo", "body")
    p = root / "wiki" / "entities" / "f" / "foo.md"
    fm = yaml.safe_load(p.read_text().split("---")[1])
    assert fm["created"] == "2026-06-15" and fm["version"] == 1
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-20")
    m._update("entities/f/foo", "type: entity\nname: Foo 2", None)
    fm2 = yaml.safe_load(p.read_text().split("---")[1])
    assert fm2["created"] == "2026-06-15"        # immutable across updates
    assert fm2["last_updated"] == "2026-06-20"    # moves
    assert fm2["version"] == 2


def test_update_clears_body_on_empty_keeps_on_none(vault):
    """okengine#52: body='' intentionally CLEARS the page body; body=None KEEPS it — so the
    update_entity wrapper (now passing body through) can clear a body, not silently keep it."""
    m, root = vault
    m._create("entities/f/foo", "type: entity\nname: Foo", "original body text")
    p = root / "wiki" / "entities" / "f" / "foo.md"
    m._update("entities/f/foo", None, None)                  # body=None -> keep
    assert "original body text" in p.read_text()
    m._update("entities/f/foo", None, "")                    # body="" -> clear
    txt = p.read_text()
    assert "original body text" not in txt
    assert txt.split("---", 2)[2].strip() == ""              # body emptied


_NS_SCHEMA = """\
okf:
  required: [type]
types:
  source: {required: [type, source_kind, published]}
  entity: {required: [type, name]}
strict_types: false
partitioning:
  namespaces:
    sources:  {strategy: by-date, date_field: published}
    entities: {strategy: by-letter}
exclude:
  - wiki/operational/
permissions:
  default: {create: true, update: true, delete: false}
"""


@pytest.fixture
def ns_vault(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(_NS_SCHEMA, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-15")
    sys.modules.pop("write_server", None)
    return _load(), tmp_path


def test_create_rejects_stray_namespace(ns_vault):
    """okengine#115: a type:source page must land in the schema's `sources/`; a stray
    `source/` (singular) is refused with a hint, so content can't fork into a tree the
    dashboards/index never see (the duplicate-namespace bug found dogfooding)."""
    m, root = ns_vault
    res = m._create("source/some-paper",
                    "type: source\nsource_kind: paper\npublished: 2026-06-01", "# Some Paper")
    assert res.startswith("rejected"), res
    assert "not declared" in res and "sources" in res    # names the issue + the right namespace
    assert not (root / "wiki" / "source").exists()        # nothing written to the stray namespace


def test_create_allows_declared_namespace(ns_vault):
    """The canonical `sources/` namespace is accepted — the guard only blocks strays."""
    m, root = ns_vault
    res = m._create("sources/2026-06-01-some-paper",
                    "type: source\nsource_kind: paper\npublished: 2026-06-01", "# Some Paper")
    assert res.startswith("created"), res


def test_create_allows_excluded_namespace(ns_vault):
    """Engine-internal excluded dirs (operational/) are not knowledge strays — not blocked."""
    m, root = ns_vault
    res = m._create("operational/health", "type: dashboard\ntitle: Health", "# Health")
    assert "not declared" not in res, res


# --- frontmatter reference normalization (wikilink -> plain path) -----------------
# Agents write [[wikilinks]]; in YAML `[[x]]` is a nested flow sequence, so a bare wikilink in a
# frontmatter value mangles. _coerce_fm canonicalizes refs to plain paths at the enforced-write
# chokepoint, fixing every extension at once. (okengine#145 follow-up)

def test_normalize_refs_strips_bare_wikilink_string():
    m = _load()
    fm = m._normalize_refs({"field_mapped": "[[concepts/x]]", "title": "Plain Title"})
    assert fm["field_mapped"] == "concepts/x"
    assert fm["title"] == "Plain Title"            # non-wikilink string untouched


def test_normalize_refs_flattens_yaml_mangled_nested_lists():
    m = _load()
    fm = m._normalize_refs({
        "field_mapped": [["concepts/supply-chain-compromise"]],   # bare [[x]] -> [["x"]]
        "see_also": [[["concepts/x"]], [["entities/s/y"]]],       # list of bare [[..]] items
    })
    assert fm["field_mapped"] == ["concepts/supply-chain-compromise"]
    assert fm["see_also"] == ["concepts/x", "entities/s/y"]


def test_normalize_refs_strips_wikilinks_in_flat_list():
    m = _load()
    fm = m._normalize_refs({"see_also": ["[[concepts/a]]", "[[entities/b]]"]})
    assert fm["see_also"] == ["concepts/a", "entities/b"]


def test_normalize_refs_leaves_plain_values_untouched():
    m = _load()
    fm = m._normalize_refs({"aliases": ["foo", "bar"], "sources": [], "name": "Acme"})
    assert fm["aliases"] == ["foo", "bar"]
    assert fm["sources"] == []
    assert fm["name"] == "Acme"


def test_coerce_fm_normalizes_bare_wikilinks_from_yaml_string():
    m = _load()
    # the REAL path: agent writes bare [[..]] in the YAML string -> safe_load mangles to nested
    # lists -> _coerce_fm must return canonical plain paths.
    yaml_text = ("type: lacuna\nfield_mapped: [[concepts/x]]\n"
                 "see_also:\n- [[concepts/a]]\n- [[entities/s/b]]\n")
    fm = m._coerce_fm(yaml_text)
    assert fm["field_mapped"] == ["concepts/x"]
    assert fm["see_also"] == ["concepts/a", "entities/s/b"]


# --- ISO-8601 timestamps for last_updated/created (OKF envelope; UI tracks WHEN, not just day) ---

def test_now_helper_timestamp_and_overrides(monkeypatch):
    m = _load()
    monkeypatch.setenv("OKENGINE_MCP_WRITE_NOW", "2026-06-28T14:30:00Z")
    assert m._now() == "2026-06-28T14:30:00Z"
    monkeypatch.delenv("OKENGINE_MCP_WRITE_NOW", raising=False)
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-28")     # date override still honored
    assert m._now() == "2026-06-28"
    monkeypatch.delenv("OKENGINE_MCP_WRITE_DATE", raising=False)
    real = m._now()                                                  # real UTC timestamp
    assert real.endswith("Z") and "T" in real and len(real) == 20


def test_create_stamps_iso_timestamp(vault, monkeypatch):
    m, root = vault
    monkeypatch.setenv("OKENGINE_MCP_WRITE_NOW", "2026-06-28T14:30:00Z")
    res = m._create("entities/q/qilin", "type: entity\nname: Qilin", "body")
    assert res.startswith("created"), res
    import yaml
    fm = yaml.safe_load((root / "wiki" / "entities" / "q" / "qilin.md").read_text().split("---")[1])
    assert fm["last_updated"] == "2026-06-28T14:30:00Z"   # a timestamp, not just a date
    assert fm["created"] == "2026-06-28T14:30:00Z"
