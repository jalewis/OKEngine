"""okengine#961: a composed `cron:<job>@<pack>` write actor is held to its engine lane's checks.

cron_pack_split.merge_packs names a composed engine-template instance `<job>@<pack>`, and
ensure-runtime binds that writer as `OKENGINE_WRITE_ACTOR: cron:<job>@<pack>`. Every write-path
check keyed by an engine lane name compared the raw actor, so a composed lane silently got the full
write-tool surface (update/patch/create/tombstone instead of only its converge tool), lost its
lane-specific tool and receipts, and was judged against the generic contract-derived capability
instead of the lane's catalog capability. `write_services.state.lane_actor` is the one normalizer; these tests
pin every consumer to it, and pin that a suffix never grants a different lane's surface.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WRITE = REPO / "okengine-mcp" / "write_server.py"
SPLIT = REPO / "scripts" / "cron_pack_split.py"

sys.path.insert(0, str(REPO / "okengine-mcp"))
import converge  # noqa: E402
from okengine.write_services.state import lane_actor  # noqa: E402

_FULL_SURFACE = {"create_entity", "update_entity", "patch_entity", "tombstone_entity"}


def _load(tmp_path: Path, monkeypatch, actor: str):
    pytest.importorskip("mcp")
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", actor)
    monkeypatch.delenv("OKENGINE_WRITE_TOOLS", raising=False)
    name = "ws_961_" + "".join(c if c.isalnum() else "_" for c in actor) + f"_{id(tmp_path)}"
    spec = importlib.util.spec_from_file_location(name, WRITE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _tools(module) -> set[str]:
    return {t.name for t in asyncio.run(module.mcp.list_tools())}


def _tool(module, name):
    return module.mcp._tool_manager._tools[name].fn


# --- the normalizer -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("actor", "lane"),
    [
        ("cron:entity-backfill@okpack-threat-actors", "cron:entity-backfill"),
        ("cron:entity-backfill", "cron:entity-backfill"),
        # Only the FIRST `@` separates the pack: the lane part never contains one.
        ("cron:entity-backfill@okpack-a@b", "cron:entity-backfill"),
        # Domain (`<pack>:<job>`) and extension (`<ext>:<op>`) actors carry no suffix.
        ("cron:okpack-a:entity-backfill", "cron:okpack-a:entity-backfill"),
        ("cron:okengine.predictions:prediction-structural-backfill",
         "cron:okengine.predictions:prediction-structural-backfill"),
        ("admin", "admin"),
        ("", ""),
        (None, ""),
    ],
)
def test_lane_actor_removes_only_the_composed_pack_suffix(actor, lane):
    assert lane_actor(actor) == lane


def test_additive_gate_still_matches_a_composed_lane_through_the_normalizer():
    """The #962 behavior that already stripped the suffix now goes through the shared helper."""
    assert converge.lane_actor is lane_actor, "converge must use the one write-path normalizer"
    prev, new = {"title": "A", "description": "curated"}, {"description": "replacement"}
    _, _, additive, _ = converge.additive_gate("cron:entity-backfill@okpack-a", prev, new, "")
    assert additive is True
    _, _, additive, _ = converge.additive_gate("cron:page-quality-enrich@okpack-a", prev, new, "")
    assert additive is False


def test_the_write_path_and_the_generator_agree_on_every_composed_name():
    """The `@<pack>` convention is minted in cron_pack_split and read on the write path. A
    divergence would reopen #961 without failing any single-surface test."""
    spec = importlib.util.spec_from_file_location("cron_pack_split_961", SPLIT)
    assert spec and spec.loader
    cps = importlib.util.module_from_spec(spec)
    sys.modules["cron_pack_split_961"] = cps
    spec.loader.exec_module(cps)
    tier_of = cps._tier_map(cps.TIERS)
    templates = sorted(name for name, tier in tier_of.items() if tier == "engine-template")
    assert "entity-backfill" in templates
    engine = [{"name": name, "prompt": "", "enabled": True} for name in templates]
    packs = [{"name": "okpack-a", "domain": [], "prompts": {n: "Do the lane." for n in templates}}]
    jobs, errors = cps.merge_packs(engine, packs, tier_of)
    assert errors == []
    composed = [job["name"] for job in jobs if "@" in job["name"]]
    assert len(composed) == len(templates)
    for name in composed:
        base = cps.template_lane(name)
        assert base in templates
        assert lane_actor(f"cron:{name}") == f"cron:{base}"


# --- the tool fence -------------------------------------------------------------------------


def _engine_lanes():
    from okengine.write_services.authorization import ACTOR_TOOLS

    return sorted(ACTOR_TOOLS.items())


@pytest.mark.parametrize(("lane", "allowed"), _engine_lanes())
def test_a_composed_engine_lane_sees_exactly_its_engine_tool_surface(
    tmp_path, monkeypatch, lane, allowed
):
    plain = _tools(_load(tmp_path / "plain", monkeypatch, lane))
    composed = _tools(_load(tmp_path / "composed", monkeypatch, f"{lane}@okpack-a"))
    assert plain <= allowed and plain, f"{lane}: the plain lane must keep its engine surface"
    assert composed == plain, (
        f"{lane}@okpack-a got {sorted(composed)}, but its engine lane is fenced to "
        f"{sorted(plain)} (okengine#961)"
    )
    assert not composed & (_FULL_SURFACE - allowed)


def test_the_issue_reproduction_entity_backfill_loses_update_entity(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "cron:entity-backfill@okpack-a")
    assert _tools(m) == {"converge_entity"}


def test_a_pack_suffix_never_grants_another_lanes_surface(tmp_path, monkeypatch):
    """The suffix names a PACK. A pack named after a lane does not make the actor that lane."""
    m = _load(tmp_path / "a", monkeypatch, "cron:entity-backfill@concept-backfill")
    assert _tools(m) == {"converge_entity"}
    # A non-engine lane whose PACK is named like an engine lane keeps the full, policy-guarded
    # surface it always had; it is not narrowed into (or granted) that lane's tool.
    m = _load(tmp_path / "b", monkeypatch, "cron:some-pack-lane@entity-backfill")
    assert _FULL_SURFACE <= _tools(m)
    assert "converge_concept" not in _tools(m) and "converge_source" not in _tools(m)


def test_the_fence_holds_without_the_optional_converge_lib(tmp_path, monkeypatch):
    """The facade's converge import is optional (a host without it keeps the plain tools). The
    composed fence must not depend on it, or such a host would fail open or crash at import."""
    from okengine.write_services import authorization  # noqa: F401 - preload before blocking
    monkeypatch.setitem(sys.modules, "converge", None)
    m = _load(tmp_path, monkeypatch, "cron:page-quality-enrich@okpack-a")
    assert m._CONVERGE_OK is False
    assert _tools(m) == {"update_entity", "patch_entity", "append_to_section", "converge_entity"}


def test_a_domain_job_named_after_an_engine_lane_is_not_that_lane(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "cron:okpack-a:entity-backfill")
    assert _FULL_SURFACE <= _tools(m)


# --- lane-specific tools and receipts -------------------------------------------------------


def test_composed_concept_backfill_keeps_its_path_pin_and_type_forcing(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "cron:concept-backfill@okpack-a")
    converge_concept = _tool(m, "converge_concept")
    assert "only concepts" in converge_concept("entities/a")
    captured = {}

    def write(path, frontmatter, *_args):
        captured.update(path=path, frontmatter=frontmatter)
        return "wrote"

    monkeypatch.setattr(m, "_converge", write)
    assert "SUCCESS:" in converge_concept("concepts/my-topic", "type: actor")
    assert "type: concept" in captured["frontmatter"]


def test_composed_raw_backfill_keeps_its_sources_only_tool(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "cron:raw-backfill@okpack-a")
    assert "only sources" in _tool(m, "converge_source")("entities/a", {})


def test_composed_entity_backfill_sanitizes_and_returns_receipt_guidance(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "cron:entity-backfill@okpack-a")
    calls = []

    def sanitize(path, frontmatter_yaml, body=""):
        calls.append(frontmatter_yaml)
        return "type: actor", None

    monkeypatch.setattr(m, "_entity_backfill_frontmatter", sanitize)
    monkeypatch.setattr(m, "_converge", lambda *_a: "wrote")
    assert "SUCCESS:" in _tool(m, "converge_entity")("entities/a", "id: sources:x")
    assert calls == ["id: sources:x"], "the source-id sanitizer must run for a composed lane"
    monkeypatch.setattr(m, "_converge", lambda *_a: "refused: conflict")
    assert "TERMINAL:" in _tool(m, "converge_entity")("entities/a", "type: actor")


def test_composed_quality_lanes_return_their_receipt_guidance(tmp_path, monkeypatch):
    quality = _load(tmp_path / "q", monkeypatch, "cron:source-quality-backfill@okpack-a")
    monkeypatch.setattr(quality, "_update", lambda *_a: "wrote")
    assert "SUCCESS:" in _tool(quality, "score_source")("sources/a", "a", 1)
    page = _load(tmp_path / "p", monkeypatch, "cron:page-quality-enrich@okpack-a")
    monkeypatch.setattr(page, "_append_section", lambda *_a: "wrote")
    assert "exact fenced okengine-receipt" in _tool(page, "append_to_section")("p", "H", "t")


def test_receipt_guidance_is_an_exact_lane_match(tmp_path, monkeypatch):
    """A lane that is not source-quality or page-quality, sorting AFTER both names, gets the
    plain tool result: the guidance is keyed by lane identity, never by ordering."""
    m = _load(tmp_path, monkeypatch, "cron:zz-pack-lane@okpack-a")
    monkeypatch.setattr(m, "_update", lambda *_a: "wrote")
    monkeypatch.setattr(m, "_append_section", lambda *_a: "wrote")
    assert _tool(m, "score_source")("sources/a", "a", 1) == "wrote"
    assert _tool(m, "append_to_section")("p", "H", "t") == "wrote"


def test_a_non_engine_lane_gets_no_lane_specific_behavior(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "cron:okpack-a:entity-backfill")
    monkeypatch.setattr(
        m, "_entity_backfill_frontmatter", lambda *_a: pytest.fail("sanitizer must not run")
    )
    monkeypatch.setattr(m, "_converge", lambda *_a: "wrote")
    assert _tool(m, "converge_entity")("entities/a", "type: actor") == "wrote"
    assert "converge_concept" not in _tools(m) and "converge_source" not in _tools(m)


# --- the catalog capability -----------------------------------------------------------------


@pytest.fixture
def quality_policy(tmp_path, monkeypatch):
    """A writer whose lane contract would derive a WIDE capability (any field on a source), so the
    narrow catalog capability (reliability/credibility only) is observable."""
    jobs = tmp_path / "jobs.json"
    contract = {"allowed_namespaces": ["sources"], "allowed_types": ["source"],
                "operations": ["update"]}
    jobs.write_text(json.dumps({"jobs": [
        {"name": "source-quality-backfill@okpack-a", "output_contract": contract},
        {"name": "okpack-a:source-quality-backfill", "output_contract": contract},
    ]}))
    monkeypatch.setenv("OKENGINE_CRON_JOBS", str(jobs))
    m = _load(tmp_path / "vault", monkeypatch, "cron:source-quality-backfill@okpack-a")
    assert "cron:source-quality-backfill" in m._effective_policy()["capabilities"]
    page = tmp_path / "vault" / "wiki" / "sources" / "a.md"

    def reject(actor: str, fields: list[str]):
        token = m._caller_var.set({"kind": "job", "actor": actor})
        try:
            return m._capability_reject(page, "update", page_type="source", changed_fields=fields)
        finally:
            m._caller_var.reset(token)

    return m, reject


def test_a_composed_lane_inherits_its_lanes_narrow_catalog_capability(quality_policy):
    _, reject = quality_policy
    assert reject("cron:source-quality-backfill@okpack-a", ["reliability"]) is None
    refused = reject("cron:source-quality-backfill@okpack-a", ["title"])
    assert refused and "source-quality-fields-only" in refused, refused


def test_a_composed_lanes_own_capability_entry_takes_precedence(quality_policy, monkeypatch):
    m, reject = quality_policy
    policy = dict(m._effective_policy())
    caps = dict(policy["capabilities"])
    caps["cron:source-quality-backfill@okpack-a"] = {
        **caps["cron:source-quality-backfill"], "update_fields": ["title"],
    }
    policy["capabilities"] = caps
    monkeypatch.setattr(m, "_effective_policy", lambda: policy)
    assert reject("cron:source-quality-backfill@okpack-a", ["title"]) is None


def test_a_domain_job_does_not_inherit_an_engine_lanes_capability(quality_policy):
    """Negative: `<pack>:<job>` is not a composed instance, so it keeps the contract-derived
    capability rather than borrowing the engine lane's."""
    _, reject = quality_policy
    assert reject("cron:okpack-a:source-quality-backfill", ["title"]) is None


def test_an_empty_lane_capability_fails_closed(quality_policy, monkeypatch):
    m, reject = quality_policy
    policy = dict(m._effective_policy())
    policy["capabilities"] = {**policy["capabilities"], "cron:source-quality-backfill": {}}
    monkeypatch.setattr(m, "_effective_policy", lambda: policy)
    assert reject("cron:source-quality-backfill@okpack-a", ["reliability"])


# --- the generator --------------------------------------------------------------------------


def test_a_composed_source_quality_lane_gets_the_rubric_stamp():
    spec = importlib.util.spec_from_file_location("cron_pack_split_961b", SPLIT)
    assert spec and spec.loader
    cps = importlib.util.module_from_spec(spec)
    sys.modules["cron_pack_split_961b"] = cps
    spec.loader.exec_module(cps)
    stamped = cps._stamp_mcp_discovery_contract(
        {"name": "source-quality-backfill@okpack-a", "prompt": "Score the source."}
    )
    assert "SOURCE QUALITY RUBRIC:" in stamped["prompt"]
    other = cps._stamp_mcp_discovery_contract(
        {"name": "okpack-a:source-quality-backfill", "prompt": "Score the source."}
    )
    assert "SOURCE QUALITY RUBRIC:" not in other["prompt"]
    later = cps._stamp_mcp_discovery_contract(
        {"name": "trends-refresh@okpack-a", "prompt": "Summarize trends."}
    )
    assert "SOURCE QUALITY RUBRIC:" not in later["prompt"], "rubric is keyed by exact lane"
