"""okengine.lacuna — structural-gap discovery (okengine#145).

A schema-OWNING agent extension (like glossary): it owns the `lacuna` type + namespace and
writes low-trust analysis pages there, behind a concept-cluster-density wake-gate. These guard
that it ships well-formed (discovers, validates, owns its schema, gates correctly) and that the
isolated selector stays self-contained.
"""
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.lacuna"
COMPOSE = REPO / "scripts" / "extension_compose.py"
MANIFEST = REPO / "scripts" / "extension_manifest.py"
SCHEMA_LIB = REPO / "scripts" / "cron" / "schema_lib.py"
SELECTOR = EXT / "select_lacuna_field.py"

pytestmark = pytest.mark.skipif(not EXT.is_dir(), reason="okengine.lacuna absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _manifest():
    return yaml.safe_load((EXT / "extension.yaml").read_text(encoding="utf-8"))


# --- manifest -------------------------------------------------------------

def test_manifest_valid_first_party_opt_in():
    mod = _load("extension_manifest", MANIFEST)
    m = _manifest()
    assert m["id"] == "okengine.lacuna" and mod.is_reserved_id(m["id"])
    assert m.get("core") is not True                  # opt-in (spends model budget)
    assert m["requires"]["schema_refs"] == ["concept", "entity"]
    errors, _ = mod.validate_manifest(m)
    assert not errors, errors


def test_soft_predictions_edge_not_a_hard_require():
    """The predictions integration is a SOFT convention (write into predictions/**), never a
    hard dependency — lacuna must run with predictions absent."""
    m = _manifest()
    assert "predictions/**" in m["capabilities"]["write"]
    assert "predictions" not in str((m["requires"].get("extensions") or ""))


def test_soft_predictions_edge_specifies_horizon_math():
    """A lacuna-filed prediction must classify horizon by computed day-count, not by feel —
    a vague 'file a falsifiable, dated prediction' instruction (with no rubric) let the agent
    invent field names (prediction_date/resolution_date instead of made_on/resolves_by) and
    miscompute horizon (915 days tagged 'medium' instead of 'long') across most of a live
    10-prediction sample. The prompt must pin the exact field names and the short/medium/
    long/strategic day-count boundaries."""
    prompt = (EXT / "prompts" / "lacuna.md").read_text()
    for field in ("made_on", "resolves_by", "horizon", "confidence"):
        assert field in prompt, f"prompt missing required field name {field!r}"
    for boundary in ("90", "365", "1825"):
        assert boundary in prompt, f"prompt missing horizon day-count boundary {boundary!r}"


def test_composes_into_one_agent_job_in_analyze_tier():
    c = _load("extension_compose", COMPOSE)
    rec = {"id": "okengine.lacuna", "tier": "engine", "dir": str(EXT), "manifest": _manifest()}
    jobs, errors, _ = c.synthesize_ops(rec)
    assert not errors, errors
    assert len(jobs) == 1
    j = jobs[0]
    assert j["name"] == "okengine.lacuna" and j["no_agent"] is False
    assert j["prompt"].strip() and j["tier"] == "analyze"
    assert j["script"].endswith("select_lacuna_field.py")
    # ships DAILY by default (drift-gated, so a no-op day is cheap) — a deployment can still
    # override per-pack via .okengine/extension-schedules.json. Guard against a silent regression
    # back to the old weekly cadence.
    assert (j.get("schedule") or {}).get("expr") == "0 6 * * *", j.get("schedule")


def test_config_block_present():
    m = _manifest()
    cfg = m["config"]
    assert cfg["min_density"]["default"] == 8
    assert cfg["reanalyze_days"]["default"] == 90
    assert cfg["batch_size"]["default"] == 3


# --- bring-your-own-schema: OWN the lacuna type (#133) --------------------

def _frag():
    return yaml.safe_load((EXT / "schema" / "lacuna.schema.yaml").read_text())


def _base_pack(root: Path, extra_types=None, extra_ns=None):
    types = {"entity": {"required": ["type"]}, "concept": {"required": ["type"]}}
    types.update(extra_types or {})
    ns = {"entities": {}, "concepts": {}}
    ns.update(extra_ns or {})
    (root / "schema.yaml").write_text(yaml.safe_dump({
        "okf": {"required": ["type"]}, "types": types,
        "partitioning": {"namespaces": ns}}), encoding="utf-8")


def test_fragment_owns_lacuna_type_and_namespace(tmp_path):
    sl = _load("schema_lib", SCHEMA_LIB)
    _base_pack(tmp_path)
    composed, errors = sl.compose_schema(tmp_path, [("ext:okengine.lacuna", _frag())])
    assert not errors, errors
    assert "lacuna" in composed["types"]
    assert composed["owners"]["types"]["lacuna"] == "ext:okengine.lacuna"
    assert "lacuna" in composed["partitioning"]["namespaces"]
    assert composed["owners"]["namespaces"]["lacuna"] == "ext:okengine.lacuna"
    # the load-bearing fields are MUSTs
    assert set(composed["types"]["lacuna"]["required"]) == {
        "type", "field_mapped", "hidden_axis", "force", "fill"}


def test_own_collides_with_a_pack_that_already_has_lacuna(tmp_path):
    """Own = new ids only: a pack already owning `lacuna` makes the fragment fail loud."""
    sl = _load("schema_lib", SCHEMA_LIB)
    _base_pack(tmp_path, extra_types={"lacuna": {"required": ["type"]}})
    _, errors = sl.compose_schema(tmp_path, [("ext:okengine.lacuna", _frag())])
    assert any("lacuna" in e and "already owned" in e for e in errors), errors


def test_manifest_wires_the_schema_fragment_into_the_composer():
    """The owned fragment must be DECLARED in the manifest `schema:` list. The composer loads
    fragments ONLY from `manifest["schema"]` (extension_compose._fragments_from_resolved) — an
    absent declaration silently orphans the file: the `lacuna` type never enters any vault's
    composed schema and the agent's `type: lacuna` writes are rejected at runtime. This is the
    one path the in-isolation `_frag()` tests above don't cover (they read the file directly)."""
    m = _manifest()
    assert "schema/lacuna.schema.yaml" in (m.get("schema") or []), \
        "manifest must DECLARE its schema fragment in `schema:` or the composer skips it"
    c = _load("extension_compose", COMPOSE)
    resolved = {"okengine.lacuna": {"manifest": m, "dir": str(EXT)}}
    frags, errors = c._fragments_from_resolved(resolved)
    assert not errors, errors
    by_owner = dict(frags)
    assert "ext:okengine.lacuna" in by_owner, "fragment was not loaded via the manifest"
    assert "lacuna" in by_owner["ext:okengine.lacuna"]["owns"]["types"]


# --- wake-gate selector ---------------------------------------------------

def _run_gate(vault: Path, **env):
    e = {**os.environ, "WIKI_PATH": str(vault), "OKENGINE_MCP_WRITE_DATE": "2026-06-26", **env}
    return subprocess.run([sys.executable, str(SELECTOR)],
                          capture_output=True, text=True, env=e).stdout


def _page(vault: Path, rel: str, body: str):
    p = vault / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _dense_field(vault: Path, slug="latency", n=10):
    _page(vault, f"concepts/{slug}.md", f"---\ntype: concept\n---\nThe {slug} theme.\n")
    for i in range(n):
        _page(vault, f"entities/{slug}-e{i}.md", f"Uses [[concepts/{slug}]].\n")


def test_gate_wakes_on_a_dense_unanalyzed_field(tmp_path):
    _dense_field(tmp_path)                       # 10 refs >= default min_density 8
    out = _run_gate(tmp_path)
    assert '"wakeAgent": true' in out and "latency" in out
    assert "surround_density" in out             # the measured signal is surfaced


def _dense_field_sharded(vault: Path, slug="ransomware", n=10):
    """Concept page + inbound links in the SHARDED layout (concepts/<shard>/<slug>) that large
    vaults use for scale. The original flat-only regex (`[[concepts/<slug>]]`) missed these, so
    the wake-gate counted ~0 and never fired on a real (sharded) vault (okengine#145 follow-up)."""
    shard = slug[0]
    _page(vault, f"concepts/{shard}/{slug}.md", f"---\ntype: concept\n---\nThe {slug} theme.\n")
    for i in range(n):
        _page(vault, f"entities/{shard}/{slug}-e{i}.md", f"Uses [[concepts/{shard}/{slug}]].\n")


def test_gate_counts_sharded_concept_links(tmp_path):
    """The real-vault shape that shipped broken: sharded concept paths/links must be counted,
    and the cluster keyed by the FINAL slug (not the shard letter)."""
    _dense_field_sharded(tmp_path)               # 10 sharded refs >= min_density 8
    out = _run_gate(tmp_path)
    assert '"wakeAgent": true' in out, out
    assert "concept: ransomware" in out          # keyed by final slug, not the shard "r"


def test_gate_folds_flat_and_sharded_links_into_one_cluster(tmp_path):
    """A concept referenced both ways (`[[concepts/x]]` and `[[concepts/s/x]]`) is ONE field —
    densities must sum, not split (the sec-vault case: supply-chain-compromise = 6 flat + 6 sharded)."""
    _page(tmp_path, "concepts/s/supply-chain.md", "---\ntype: concept\n---\nSC.\n")
    for i in range(4):                            # flat form
        _page(tmp_path, f"entities/flat-{i}.md", "See [[concepts/supply-chain]].\n")
    for i in range(4):                            # sharded form, same concept
        _page(tmp_path, f"sources/shard-{i}.md", "See [[concepts/s/supply-chain]].\n")
    out = _run_gate(tmp_path)                     # 4+4 = 8 distinct pages >= min_density 8
    assert '"wakeAgent": true' in out, out
    assert "supply-chain" in out


def test_gate_stays_quiet_when_field_is_thin(tmp_path):
    _dense_field(tmp_path, n=3)                  # 3 < 8
    assert '"wakeAgent": false' in _run_gate(tmp_path)


def test_gate_excludes_recently_analyzed_but_refreshes_old(tmp_path):
    _dense_field(tmp_path)
    _page(tmp_path, "lacuna/tail-latency.md",
          "---\ntype: lacuna\nupdated: 2026-06-20\n---\nField [[concepts/latency]].\n")
    assert '"wakeAgent": false' in _run_gate(tmp_path)          # analyzed 6 days ago -> quiet
    # an old lacuna page (beyond reanalyze_days) -> the field is eligible again
    _page(tmp_path, "lacuna/tail-latency.md",
          "---\ntype: lacuna\nupdated: 2025-01-01\n---\nField [[concepts/latency]].\n")
    assert '"wakeAgent": true' in _run_gate(tmp_path)


def test_gate_requires_a_real_concept_page(tmp_path):
    """A dense but dangling [[concepts/<slug>]] (no concept page) is a coverage gap, not a
    mappable field — not surfaced."""
    for i in range(10):
        _page(tmp_path, f"entities/x{i}.md", "Links [[concepts/ghost]].\n")
    assert '"wakeAgent": false' in _run_gate(tmp_path)


# --- isolation ------------------------------------------------------------

def test_selector_is_self_contained():
    """The selector runs from its own /opt/data/scripts/<id>/ dir, so it must not import
    sibling engine cron libs (only stdlib + yaml)."""
    src = SELECTOR.read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([a-zA-Z_][\w.]*)", src, re.M)
    allowed = {"__future__", "json", "os", "re", "sys", "collections", "dataclasses",
               "datetime", "pathlib", "yaml", "typing", "itertools", "math", "functools"}
    foreign = [i for i in imports if i.split(".")[0] not in allowed]
    assert not foreign, f"extension script imports non-stdlib siblings: {foreign}"
