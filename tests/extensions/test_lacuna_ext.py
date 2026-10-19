"""okengine.lacuna — structural-gap discovery (okengine#145).

A schema-OWNING agent extension (like glossary): it owns the `lacuna` type + namespace and
writes low-trust analysis pages there, behind a concept-cluster-density wake-gate. These guard
that it ships well-formed (discovers, validates, owns its schema, gates correctly) and that the
isolated selector stays self-contained.
"""
import importlib.util
import json
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
    assert (j.get("schedule") or {}).get("expr") == "@jitter:daily@6", j.get("schedule")


def test_config_block_present():
    m = _manifest()
    cfg = m["config"]
    assert cfg["min_density"]["default"] == 8
    assert cfg["reanalyze_days"]["default"] == 90
    assert cfg["batch_size"]["default"] == 3
    assert cfg["nominations_path"]["default"] == ".okengine/lacuna-nominations.json"
    assert cfg["nomination_max_age_days"]["default"] == 7
    assert cfg["focus"]["default"] == "" and cfg["focus"]["type"] == "string"   # unset = autonomous


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


def _nominate(vault: Path, field: str, *, nominated_at="2026-06-26"):
    path = vault / ".okengine" / "lacuna-nominations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"nominations": [{
        "field": field,
        "source": "sources/2026/06/26/material-source",
        "reason": "Material evidence may change the field boundary.",
        "nominated_at": nominated_at,
    }]}), encoding="utf-8")


def _write_nomination_payload(vault: Path, payload):
    path = vault / ".okengine" / "lacuna-nominations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_fresh_material_nomination_precedes_denser_rotation_candidate(tmp_path):
    _dense_field(tmp_path, slug="latency", n=12)
    _dense_field_sharded(tmp_path, slug="agentic-ai-security", n=8)
    _nominate(tmp_path, "[[concepts/a/agentic-ai-security]]")

    out = _run_gate(tmp_path, OKENGINE_LACUNA_BATCH_SIZE="1")

    assert '"wakeAgent": true' in out
    assert "concept: agentic-ai-security" in out
    assert "material-source nomination" in out
    assert "concept: latency" not in out


def test_nomination_cannot_bypass_density_gate(tmp_path):
    """Negative gate: a routed story must not turn a thin field into confident inference."""
    _dense_field_sharded(tmp_path, slug="agentic-ai-security", n=7)
    _nominate(tmp_path, "concepts/a/agentic-ai-security")

    out = _run_gate(tmp_path)

    assert '"wakeAgent": false' in out
    assert "0 eligible" in out


@pytest.mark.parametrize("nominated_at", ["2026-06-18", "invalid", "2026-06-27"])
def test_stale_malformed_or_future_nomination_does_not_change_rotation(tmp_path, nominated_at):
    _dense_field(tmp_path, slug="latency", n=10)
    _dense_field_sharded(tmp_path, slug="agentic-ai-security", n=8)
    _nominate(tmp_path, "concepts/a/agentic-ai-security", nominated_at=nominated_at)

    out = _run_gate(tmp_path, OKENGINE_LACUNA_BATCH_SIZE="1")

    assert "concept: latency" in out
    assert "material-source nomination" not in out


@pytest.mark.parametrize(
    ("payload", "path_override"),
    [
        ({"nominations": {}}, None),
        ({"nominations": ["not-an-object"]}, None),
        ({"nominations": []}, ""),
    ],
)
def test_missing_or_structurally_invalid_nomination_queue_is_ignored(
        tmp_path, payload, path_override):
    _dense_field(tmp_path)
    _write_nomination_payload(tmp_path, payload)
    env = {}
    if path_override is not None:
        env["OKENGINE_LACUNA_NOMINATIONS_PATH"] = path_override

    out = _run_gate(tmp_path, **env)

    assert '"wakeAgent": true' in out
    assert "fresh material nominations: 0" in out


def test_absolute_queue_keeps_newest_duplicate_and_omits_empty_context(tmp_path):
    _dense_field(tmp_path)
    path = _write_nomination_payload(tmp_path, {"nominations": [
        {
            "field": "concepts/latency",
            "source": "",
            "reason": "",
            "nominated_at": "2026-06-26",
        },
        {
            "field": "concepts/latency",
            "source": "older-source-must-not-win",
            "reason": "older-reason-must-not-win",
            "nominated_at": "2026-06-25",
        },
    ]})

    out = _run_gate(tmp_path, OKENGINE_LACUNA_NOMINATIONS_PATH=str(path))

    assert "material-source nomination" in out
    assert "nominated_at: 2026-06-26" in out
    assert "older-source-must-not-win" not in out
    assert "older-reason-must-not-win" not in out
    assert "    source:" not in out
    assert "    reason:" not in out


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


def test_selector_defensive_frontmatter_rotation_and_scan_edges(tmp_path, monkeypatch, capsys):
    module = _load("lacuna_selector_edges", SELECTOR)
    monkeypatch.setattr(module, "WIKI", tmp_path / "missing")
    assert module.main() == 0
    assert '"wakeAgent": false' in capsys.readouterr().out
    assert module._read_fm(tmp_path / "missing.md") == {}
    plain = tmp_path / "plain.md"
    plain.write_text("body")
    assert module._read_fm(plain) == {}
    plain.write_text("---\n[broken\n---\n")
    assert module._read_fm(plain) == {}
    plain.write_text("---\n- list\n---\n")
    assert module._read_fm(plain) == {}

    wiki = tmp_path / "wiki"
    lacuna = wiki / "lacuna"
    sources = wiki / "sources"
    concepts = wiki / "concepts"
    for directory in (lacuna, sources, concepts):
        directory.mkdir(parents=True)
    unreadable = lacuna / "unreadable.md"
    unreadable.write_text("x")
    other = lacuna / "other.md"
    other.write_text("---\ntype: report\n---\n")
    undated = lacuna / "undated.md"
    undated.write_text(
        "---\ntype: lacuna\nfield_mapped: ['', '[[concepts/a]]']\n---\n"
    )
    raced = sources / "raced.md"
    raced.write_text("[[concepts/a]]")
    own = concepts / "a.md"
    own.write_text("[[concepts/a]]")
    original_read = Path.read_text
    original_stat = Path.stat

    def flaky_read(path, *args, **kwargs):
        if path in {unreadable, raced}:
            raise OSError("vanished")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read)
    monkeypatch.setattr(
        Path, "stat",
        lambda path, *args, **kwargs: (_ for _ in ()).throw(OSError("vanished"))
        if path == undated else original_stat(path, *args, **kwargs),
    )
    monkeypatch.setattr(module, "WIKI", wiki)
    assert module._recently_analyzed() == {"a"}
    assert module._clusters() == ({}, {})


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


# --- FOCUS override (operator-pinned topic; default unset = autonomous) -----

def test_focus_unset_is_the_autonomous_default(tmp_path):
    """The knob defaults to no focus — the densest unanalyzed field, everything considered."""
    _dense_field(tmp_path, slug="latency", n=10)
    out = _run_gate(tmp_path)                                # no OKENGINE_LACUNA_FOCUS
    assert '"wakeAgent": true' in out and "concept: latency" in out
    assert "FOCUS" not in out                                # no focus banner when unset


def test_focus_pins_a_field_below_the_density_rank_with_extrapolation_warning(tmp_path):
    _dense_field(tmp_path, slug="latency", n=10)             # the densest (autonomous pick)
    _page(tmp_path, "concepts/z/niche.md", "---\ntype: concept\n---\nNiche.\n")
    for i in range(4):                                       # 4 refs < default min_density 8
        _page(tmp_path, f"entities/niche-e{i}.md", "Uses [[concepts/z/niche]].\n")
    auto = _run_gate(tmp_path)
    assert "concept: latency" in auto and "concept: niche" not in auto   # autonomous ignores the sparse field
    foc = _run_gate(tmp_path, OKENGINE_LACUNA_FOCUS="niche")
    assert '"wakeAgent": true' in foc
    assert "concept: niche" in foc and "concept: latency" not in foc     # focus pins it exclusively
    assert "EXTRAPOLATION" in foc                                        # below min_density -> warns


def test_focus_accepts_slug_path_and_wikilink_forms(tmp_path):
    _dense_field(tmp_path, slug="ransomware", n=10)
    for form in ("ransomware", "concepts/r/ransomware", "[[concepts/ransomware]]"):
        out = _run_gate(tmp_path, OKENGINE_LACUNA_FOCUS=form)
        assert "concept: ransomware" in out and '"wakeAgent": true' in out, form


def test_focus_on_missing_or_empty_field_skips_not_wakes(tmp_path):
    _dense_field(tmp_path, slug="latency", n=10)             # a real dense field exists
    miss = _run_gate(tmp_path, OKENGINE_LACUNA_FOCUS="does-not-exist")
    assert '"wakeAgent": false' in miss and "no concept page" in miss    # can't map a non-existent field
    _page(tmp_path, "concepts/o/orphan.md", "---\ntype: concept\n---\nOrphan.\n")
    empty = _run_gate(tmp_path, OKENGINE_LACUNA_FOCUS="orphan")
    assert '"wakeAgent": false' in empty and "NO referencing" in empty   # page but empty field -> skip


def test_config_keys_are_read_from_env_by_selector():
    """Local guard for lacuna's canonical config environment reads."""
    cfg = _manifest().get("config") or {}
    assert cfg, "lacuna should declare a config block"
    src = SELECTOR.read_text(encoding="utf-8")
    missing = [k for k in cfg if f"OKENGINE_LACUNA_{k.upper()}" not in src]
    assert not missing, (
        f"config keys with no OKENGINE_LACUNA_<KEY> read in {SELECTOR.name}: {missing} — the config "
        "block would silently no-op."
    )


# ── inquiry nominations (okengine#854 step 3) ───────────────────────────────

def _inquiry_nominate(vault: Path, field: str, *, nominated_at="2026-06-26"):
    path = vault / ".okengine" / "inquiry" / "lacuna-nominations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"nominations": [{
        "field": field, "source": "inquiry/ai-risk",
        "reason": "inquiry question: How likely is it?", "nominated_at": nominated_at,
    }]}), encoding="utf-8")


def test_an_inquiry_field_is_mapped_below_the_floor_with_the_extrapolation_warning(tmp_path):
    """The vault said what it is researching: that field ranks first even though it is thinner
    than the autonomous floor, and the digest says the result is extrapolation."""
    _dense_field(tmp_path, slug="latency", n=12)
    _dense_field(tmp_path, slug="safety-alignment", n=4)          # 4 < 8, >= inquiry floor 3
    _inquiry_nominate(tmp_path, "concepts/s/safety-alignment")
    out = _run_gate(tmp_path, OKENGINE_LACUNA_BATCH_SIZE="1")
    assert '"wakeAgent": true' in out
    assert "concept: safety-alignment" in out and "concept: latency" not in out
    assert "inquiry nomination — inquiry/ai-risk" in out
    assert "EXTRAPOLATION" in out
    assert "inquiry field nominations: 1 (1 eligible, floor 3)" in out


def test_an_inquiry_field_below_the_inquiry_floor_is_not_mapped(tmp_path):
    """NEGATIVE: a declaration lowers the floor, it does not remove it."""
    _dense_field(tmp_path, slug="safety-alignment", n=2)
    _inquiry_nominate(tmp_path, "safety-alignment")
    out = _run_gate(tmp_path)
    assert '"wakeAgent": false' in out and "(0 eligible, floor 3)" in out


def test_an_inquiry_field_with_no_concept_page_is_not_mapped(tmp_path):
    """NEGATIVE: lacuna maps a NAMED field, never a dangling link, whoever nominated it."""
    for i in range(5):
        _page(tmp_path, f"entities/x-{i}.md", "See [[concepts/ghost-field]].\n")
    _inquiry_nominate(tmp_path, "ghost-field")
    assert '"wakeAgent": false' in _run_gate(tmp_path)


def test_a_stale_inquiry_nomination_is_ignored(tmp_path):
    """The dossier re-dates live nominations daily; an old row means the lane stopped."""
    _dense_field(tmp_path, slug="safety-alignment", n=4)
    _inquiry_nominate(tmp_path, "safety-alignment", nominated_at="2026-06-01")
    out = _run_gate(tmp_path)
    assert '"wakeAgent": false' in out and "inquiry field nominations: 0" in out


def test_a_material_nomination_still_cannot_bypass_density_beside_an_inquiry_one(tmp_path):
    """NEGATIVE: the lower floor belongs to inquiry declarations only."""
    _dense_field(tmp_path, slug="thin-material", n=4)
    _dense_field(tmp_path, slug="safety-alignment", n=4)
    _nominate(tmp_path, "thin-material")
    _inquiry_nominate(tmp_path, "safety-alignment")
    out = _run_gate(tmp_path, OKENGINE_LACUNA_BATCH_SIZE="5")
    assert "concept: safety-alignment" in out and "concept: thin-material" not in out


def test_the_inquiry_nomination_config_is_declared():
    cfg = _manifest()["config"]
    assert cfg["inquiry_nominations_path"]["default"] == ".okengine/inquiry/lacuna-nominations.json"
    assert cfg["inquiry_min_density"]["default"] == 3


@pytest.mark.parametrize("n,warned", [(4, True), (8, False), (10, False)])
def test_the_extrapolation_warning_marks_exactly_the_fields_below_the_floor(tmp_path, n, warned):
    """At or above min_density an inquiry field is ordinary inference; only below it is the
    result extrapolation."""
    _dense_field(tmp_path, slug="safety-alignment", n=n)
    _inquiry_nominate(tmp_path, "safety-alignment")
    out = _run_gate(tmp_path, OKENGINE_LACUNA_BATCH_SIZE="1")
    assert "concept: safety-alignment" in out
    assert ("EXTRAPOLATION" in out) is warned


def test_an_inquiry_field_outranks_a_denser_material_nomination(tmp_path):
    """Declared research direction beats a routed story, even when the story's field is denser."""
    _dense_field(tmp_path, slug="material-field", n=12)
    _dense_field(tmp_path, slug="inquiry-field", n=9)
    _nominate(tmp_path, "material-field")
    _inquiry_nominate(tmp_path, "inquiry-field")
    out = _run_gate(tmp_path, OKENGINE_LACUNA_BATCH_SIZE="1")
    assert "concept: inquiry-field" in out and "concept: material-field" not in out


def test_equal_density_fields_break_ties_by_name(tmp_path):
    _dense_field(tmp_path, slug="beta-field", n=9)
    _dense_field(tmp_path, slug="alpha-field", n=9)
    out = _run_gate(tmp_path, OKENGINE_LACUNA_BATCH_SIZE="1")
    assert "concept: alpha-field" in out and "concept: beta-field" not in out


# ── density counts evidence, not derived views (okengine#855) ───────────────

def _links(vault: Path, rels, slug="field-x"):
    for rel in rels:
        _page(vault, rel, f"See [[concepts/{slug}]].\n")


def test_a_field_linked_only_by_dashboards_has_no_density(tmp_path):
    """NEGATIVE: on a live vault 4 of a field's 7 'referencing pages' were generated dashboards."""
    _page(tmp_path, "concepts/field-x.md", "---\ntype: concept\n---\nx\n")
    _links(tmp_path, [f"dashboards/d{i}.md" for i in range(12)])
    out = _run_gate(tmp_path)
    assert '"wakeAgent": false' in out and "concept clusters >= 8 refs: 0" in out


def test_only_evidence_counts_toward_density(tmp_path):
    _page(tmp_path, "concepts/field-x.md", "---\ntype: concept\n---\nx\n")
    _links(tmp_path, [f"entities/e{i}.md" for i in range(8)]
           + [f"dashboards/d{i}.md" for i in range(4)] + ["briefings/b.md", "operational/o.md",
              "lacuna/l.md", "index.md", "HOT.md", "entities/INDEX.md", "entities/INDEX-p02.md",
              "entities/_scaffold.md"])
    out = _run_gate(tmp_path)
    assert "concept: field-x  (8 referencing pages)" in out
    assert "surround_density: `8 links · entities 8`" in out


def test_derived_namespaces_are_configurable_but_lacuna_is_always_excluded(tmp_path):
    _page(tmp_path, "concepts/field-x.md", "---\ntype: concept\n---\nx\n")
    _links(tmp_path, [f"dashboards/d{i}.md" for i in range(8)] + ["lacuna/l.md"])
    out = _run_gate(tmp_path, OKENGINE_LACUNA_DENSITY_EXCLUDE_NAMESPACES="")
    assert "concept: field-x  (8 referencing pages)" in out   # dashboards counted, lacuna not


def test_the_density_exclusion_default_is_declared():
    cfg = _manifest()["config"]
    assert cfg["density_exclude_namespaces"]["default"] == "lacuna,dashboards,operational,briefings"


# --- tombstones are neither evidence nor seats -----------------------------
TOMB = "---\ntype: entity\nstatus: tombstoned\n---\n"


def test_tombstoned_citers_do_not_make_a_field_dense(tmp_path):
    """REGRESSION: tombstones keep their files, so link-counting kept counting them. On a live
    vault 18 leaked test-fixture pages were tombstoned and their links still ranked a fixture
    concept as the densest field."""
    _page(tmp_path, "concepts/fixture.md", "---\ntype: concept\n---\nx\n")
    for i in range(10):
        _page(tmp_path, f"entities/t{i}.md", TOMB + "Links [[concepts/fixture]].\n")
    assert '"wakeAgent": false' in _run_gate(tmp_path)


def test_a_tombstoned_concept_page_is_not_a_field(tmp_path):
    _dense_field(tmp_path)
    _page(tmp_path, "concepts/latency.md",
          "---\ntype: concept\nstatus: tombstoned\n---\nretired\n")
    assert '"wakeAgent": false' in _run_gate(tmp_path)


def test_a_retired_analysis_does_not_hold_its_field(tmp_path):
    """A tombstoned lacuna page (e.g. one mapped from leaked fixtures) must not keep its field
    out of rotation for 90 days."""
    _dense_field(tmp_path)
    _page(tmp_path, "lacuna/retired.md", "---\ntype: lacuna\nstatus: tombstoned\n"
          "updated: 2026-06-20\nfield_mapped: concepts/latency\n---\nretired\n")
    assert '"wakeAgent": true' in _run_gate(tmp_path)


def test_tombstone_helpers_in_process(tmp_path, monkeypatch):
    """The same rules exercised in-process, so coverage measures them (scripts/extensions are
    not packages; a subprocess-only test reads as covered while unmeasured)."""
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    mod = _load("select_lacuna_field_tomb", SELECTOR)
    assert mod._tombstoned("---\nstatus: tombstoned\n---\n")
    assert mod._tombstoned("---\nstatus: 'Tombstoned'\n---\n")
    assert not mod._tombstoned("---\nstatus: active\n---\nstatus: tombstoned in prose\n")
    assert not mod._tombstoned("no frontmatter\n")
    assert mod._has_concept_page("nothing") is False                    # no concepts/ dir
    _page(tmp_path, "concepts/a/live.md", "---\ntype: concept\n---\n")
    _page(tmp_path, "concepts/b/live.md", TOMB)                          # tombstoned twin
    _page(tmp_path, "concepts/dead.md", TOMB)
    (tmp_path / "wiki" / "concepts" / "gone.md").symlink_to(tmp_path / "missing.md")
    assert mod._has_concept_page("live") is True
    assert mod._has_concept_page("dead") is False
    assert mod._has_concept_page("gone") is False                       # unreadable
    for i in range(3):
        _page(tmp_path, f"entities/t{i}.md", TOMB + "[[concepts/live]]\n")
    _page(tmp_path, "entities/ok.md", "[[concepts/live]]\n")
    refs, _by_ns = mod._clusters()
    assert refs["live"] == {"entities/ok.md"}
    _page(tmp_path, "lacuna/r.md", "---\ntype: lacuna\nstatus: tombstoned\nupdated: 2026-06-20\n"
          "field_mapped: concepts/live\n---\n")
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-26")
    assert "live" not in mod._recently_analyzed()
    sys.modules.pop("select_lacuna_field_tomb", None)


def test_skipped_pages_never_end_a_scan_early(tmp_path, monkeypatch):
    """Every skip is a `continue`, never an early exit: retired or non-lacuna pages interleaved
    with live ones (more than any iteration order could put all after them) lose nothing."""
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-26")
    for i in range(12):
        # density: retired citers interleaved with live ones
        _page(tmp_path, f"entities/d{i:02d}/retired.md", TOMB + "[[concepts/busy]]\n")
        _page(tmp_path, f"entities/d{i:02d}/live.md", "[[concepts/busy]]\n")
        # rotation: a non-lacuna page (type sorts AFTER "lacuna") and a retired one beside each
        # real analysis; neither counts, and neither may stop the scan
        _page(tmp_path, f"lacuna/r{i:02d}/other.md", "---\ntype: source\nupdated: 2026-06-20\n"
              f"field_mapped: concepts/other-{i}\n---\n")
        _page(tmp_path, f"lacuna/r{i:02d}/retired.md", "---\ntype: lacuna\nstatus: tombstoned\n"
              f"updated: 2026-06-20\nfield_mapped: concepts/retired-{i}\n---\n")
        _page(tmp_path, f"lacuna/r{i:02d}/real.md", "---\ntype: lacuna\nupdated: 2026-06-20\n"
              f"field_mapped: concepts/mapped-{i}\n---\n")
        # concept seat: unreadable twins beside the one live page
        (tmp_path / "wiki" / "concepts" / f"s{i:02d}").mkdir(parents=True)
        (tmp_path / "wiki" / "concepts" / f"s{i:02d}" / "seat.md").symlink_to(tmp_path / "nope.md")
    _page(tmp_path, "concepts/zz/seat.md", "---\ntype: concept\n---\n")
    mod = _load("select_lacuna_field_scan", SELECTOR)
    try:
        refs, _by_ns = mod._clusters()
        assert len(refs["busy"]) == 12, "every live citer counts; retired ones never end the walk"
        analyzed = mod._recently_analyzed()
        assert {f"mapped-{i}" for i in range(12)} <= analyzed
        assert not any(s.startswith(("other-", "retired-")) for s in analyzed)
        assert mod._has_concept_page("seat") is True
    finally:
        sys.modules.pop("select_lacuna_field_scan", None)
