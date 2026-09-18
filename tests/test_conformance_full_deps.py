"""Conformance: the full-suite gate cannot SILENTLY SKIP cockpit/reader tests (okengine#75).

Some test modules gate optional application and document/render backends with `importorskip` —
they silently skip on system python but
RUN in CI (which installs those deps). A future CI edit dropping a dep, or a reviewer running
the suite on system python, silently skips them and reports a false green. This session hit
that class TWICE (main went red from tests that had skipped in local review).

This guard turns the silent skip loud:
  - When the environment DECLARES itself the full suite (OKENGINE_REQUIRE_FULL_DEPS=1, set by
    the CI full-suite job), a missing cockpit/reader dep FAILS HERE — never a quiet skip of 12
    downstream modules.
  - An inventory test catches a NEWLY-added gated module, so a new cockpit/reader test can't be
    added without acknowledging it needs the full-deps env.
"""
from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent

# Deps the cockpit/reader/framework/MCP tests gate on. Missing any in the full-suite env means
# those modules silently skip. httpx is used by the reader/cockpit TestClient path; mcp gates the
# 5 MCP-server modules INCLUDING the security-boundary regressions (test_mcp_auth's
# exposed-default-token-fails-closed, graph subprocess process-group kill) — invisible to this
# guard until 2026-07-19, so the GitLab merge gate skipped them silently while GitHub CI (which
# installs mcp) only runs post-publish (invariant-audit HIGH #10). croniter gates fleet_health;
# the remaining entries cover document extraction, DOM smoke, and Starlette's TestClient.
REQUIRED_FULL_DEPS = ("fastapi", "markdown", "nh3", "httpx", "mcp", "croniter", "docx", "pptx", "openpyxl", "striprtf", "playwright", "starlette")

# The known gated modules (okengine#75 + #10). Adding a cockpit/reader/MCP test means adding it
# here — that edit IS the acknowledgement that it needs the full-deps env to run.
EXPECTED_GATED = {
    "cron/test_fleet_health.py",
    "cron/test_observability_contract.py",
    "e2e/smoke/test_smoke_render.py",
    "extensions/test_direction_single_source.py",
    "test_backlinks_artifact_ui.py",
    "test_cockpit_config.py",
    "test_cockpit_dataset_tabs.py",
    "test_cockpit_edge_paths.py",
    "test_cockpit_grounding_registry.py",
    "test_cockpit_home.py",
    "test_cockpit_id_refs.py",
    "test_cockpit_inputshape.py",
    "test_cockpit_panels.py",
    "test_cockpit_policy.py",
    "test_cockpit_ref_link.py",
    "test_cockpit_review.py",
    "test_cockpit_sanitize.py",
    "test_cockpit_sort_then.py",
    "test_cockpit_table_drill.py",
    "test_cockpit_tid.py",
    "test_extract_docs.py",
    "test_fastapi_lifespan.py",
    "test_favicon.py",
    "test_framework_install_domain.py",
    "test_index_maintainer.py",
    "test_mcp_auth.py",
    "test_mcp_graph_hardening.py",
    "test_mcp_index_maintainer.py",
    "test_mcp_search_capacity.py",
    "test_mcp_server_contract_edges.py",
    "test_mcp_server_helpers.py",
    "test_mcp_server_resolve_key_containment.py",
    "test_okengine_mcp_backlinks.py",
    "test_reader.py",
    "test_reader_browse.py",
    "test_reader_contract_edges.py",
    "test_reader_edge_paths.py",
    "test_reader_grounding_registry.py",
    "test_reader_inputshape.py",
    "test_review_queue_newest_first.py",
    "test_write_server_review_tools_surface.py",
    "test_write_server_actor_tools_env.py",
    "test_corpus_transaction_touched.py",
}


def _gated_modules() -> set[str]:
    out = set()
    for p in TESTS.rglob("test_*.py"):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
            bindings = {}
            def literal_values(node):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    return {node.value}
                if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                    return set().union(*(literal_values(item) for item in node.elts)) if node.elts else set()
                if isinstance(node, ast.IfExp):
                    return literal_values(node.body) | literal_values(node.orelse)
                return set()
            for node in ast.walk(tree):
                if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
                    bindings[node.target.id] = literal_values(node.iter)
            deps = set()
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and node.args
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "importorskip"):
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    deps.add(arg.value.split(".", 1)[0])
                elif isinstance(arg, ast.Name):
                    deps.update(value.split(".", 1)[0] for value in bindings.get(arg.id, set()))
            if deps & set(REQUIRED_FULL_DEPS):
                out.add(p.relative_to(TESTS).as_posix())
        except (OSError, SyntaxError):
            continue
    return out


def test_full_deps_present_when_declared():
    """CI's full-suite job sets OKENGINE_REQUIRE_FULL_DEPS=1. In that env every dep the
    cockpit/reader tests gate on MUST import — a missing one FAILS here, loudly, instead of
    silently skipping the gated modules (the false-green trap that red-mained main twice)."""
    if os.environ.get("OKENGINE_REQUIRE_FULL_DEPS") != "1":
        pytest.skip(
            "full-deps not declared — system-python run. CI's full-suite job sets "
            "OKENGINE_REQUIRE_FULL_DEPS=1; locally, run cockpit/reader tests in the fastapi venv "
            "(reference-okengine-test-venv)."
        )
    missing = [d for d in REQUIRED_FULL_DEPS if importlib.util.find_spec(d) is None]
    assert not missing, (
        f"OKENGINE_REQUIRE_FULL_DEPS=1 but missing {missing} — {len(EXPECTED_GATED)} cockpit/"
        f"reader test module(s) would SILENTLY SKIP and report a false green. "
        f"Install: pip install {' '.join(missing)}"
    )


def test_gated_module_inventory_is_current():
    """Keep EXPECTED_GATED in sync with reality, so a NEW fastapi/markdown/nh3-gated test can't
    be added without being acknowledged as full-deps-requiring."""
    actual = _gated_modules()
    new = actual - EXPECTED_GATED
    assert not new, (
        f"new fastapi/markdown/nh3-gated test module(s) {sorted(new)} — add to EXPECTED_GATED "
        f"and confirm the CI full-suite job installs the deps + sets OKENGINE_REQUIRE_FULL_DEPS=1."
    )
    gone = EXPECTED_GATED - actual
    assert not gone, (
        f"gated module(s) removed/renamed: {sorted(gone)} — update EXPECTED_GATED."
    )
