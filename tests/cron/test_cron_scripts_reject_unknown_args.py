"""okengine#1034 detector: every ``scripts/cron/*.py`` with a ``main()`` rejects an unknown argument
before it touches anything.

``corpus_audit.py`` ignored argv, so a ``--help`` probe on a live host ran the whole audit as root
and rewrote the dashboard the scheduled lane owns. The class is "a cron entry point treats any
command line as 'run the lane'". The probe below runs each script as a subprocess, the way an
operator or cron-plus invokes it, inside a disposable sandbox (``WIKI_PATH``, ``HOME``, ``HERMES_HOME``
and the cwd all point into ``tmp_path``; HTTP is routed to a dead proxy) and asserts:

* exit status 2 (argparse's usage error), and
* the sandbox is unchanged (no file created, modified or removed).

Scripts that do not parse argv yet are listed in ``NOT_YET_CONVERTED`` with the reason. The list
**only shrinks**: a new cron script with a ``main()`` is probed automatically, an allowlisted
script that grows an ``argparse`` parser must leave the list (``test_converted_scripts_leave_the
_allowlist``), and ``_ALLOWLIST_CEILING`` stops new entries. Allowlisted scripts are never executed
here, because running one IS the side effect (it runs the full lane).
"""
from __future__ import annotations

import ast
import functools
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CRON = REPO / "scripts" / "cron"
PROBE_ARG = "--okengine-unknown-argument-probe"

_IGNORES_ARGV = "main() takes no argv and never parses sys.argv: any argument runs the full lane"

NOT_YET_CONVERTED: dict[str, str] = {
    "apply_curated_entity_fields.py": _IGNORES_ARGV,
    "apply_yaml_repair.py": _IGNORES_ARGV,
    "backlinks_refresh.py": _IGNORES_ARGV,
    "budget_guard.py": "main(argv) never parses argv; an unknown argument runs the guard (rc 0)",
    "build_hot_set.py": _IGNORES_ARGV,
    "build_index_tree.py": _IGNORES_ARGV,
    "collection_health.py": _IGNORES_ARGV,
    "conformance_audit.py": _IGNORES_ARGV,
    "corpus_indexer.py": _IGNORES_ARGV,
    "deployment_validate.py": _IGNORES_ARGV,
    "detect_field_loss.py": _IGNORES_ARGV,
    "ensure_readable.py": _IGNORES_ARGV,
    "fleet_health.py": _IGNORES_ARGV,
    "grounding_audit.py": _IGNORES_ARGV,
    "health_export.py": _IGNORES_ARGV,
    "id_index.py": "hand-rolled argv dispatch; an unknown argument falls through to a rebuild under /opt/data",
    "kb_health.py": _IGNORES_ARGV,
    "lint_watcher.py": _IGNORES_ARGV,
    "normalize_bare_name_links.py": _IGNORES_ARGV,
    "normalize_entity_schema.py": _IGNORES_ARGV,
    "observability_validate.py": _IGNORES_ARGV,
    "operator_dashboard.py": _IGNORES_ARGV,
    "page_quality_audit.py": _IGNORES_ARGV,
    "policy_audit.py": _IGNORES_ARGV,
    "refresh_kb_dashboards.py": _IGNORES_ARGV,
    "relink_prose_sources.py": _IGNORES_ARGV,
    "repair_broken_frontmatter.py": _IGNORES_ARGV,
    "reshelve.py": _IGNORES_ARGV,
    "review_queue.py": _IGNORES_ARGV,
    "sanitize_frontmatter_updated.py": _IGNORES_ARGV,
    "schema_drift_lint.py": _IGNORES_ARGV,
    "schema_type_drain.py": "scans sys.argv for --dry-run/--map and ignores anything else (rc 0, runs the drain)",
    "select_broken_wikilinks_drain.py": _IGNORES_ARGV,
    "select_broken_yaml.py": _IGNORES_ARGV,
    "select_concept_candidates.py": _IGNORES_ARGV,
    "select_daily_brief.py": _IGNORES_ARGV,
    "select_entity_candidates.py": _IGNORES_ARGV,
    "select_orphans_drain.py": _IGNORES_ARGV,
    "select_page_quality_enrich.py": _IGNORES_ARGV,
    "select_publisher_canonical_drain.py": _IGNORES_ARGV,
    "select_raw_batch.py": _IGNORES_ARGV,
    "select_review_drain.py": _IGNORES_ARGV,
    "select_schema_classify.py": _IGNORES_ARGV,
    "select_source_staleness.py": _IGNORES_ARGV,
    "select_trend_deltas.py": _IGNORES_ARGV,
    "select_unscored_sources.py": _IGNORES_ARGV,
    "source_backlink_drain.py": _IGNORES_ARGV,
    "source_portfolio_watch.py": _IGNORES_ARGV,
    "tier_refresh.py": _IGNORES_ARGV,
    "usage_rollup.py": "treats the first positional as the data dir; an unknown argument creates <arg>/metrics/usage.db",
    "wiki_change_check.py": "parse_known_args swallows unknown arguments (rc 0)",
    "wiki_health_audit.py": _IGNORES_ARGV,
    "year_derivation_audit.py": _IGNORES_ARGV,
}

# Lower this when you convert a script. Never raise it: a new script must parse its arguments.
_ALLOWLIST_CEILING = 53


def _has_main(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(isinstance(n, ast.FunctionDef) and n.name == "main" for n in tree.body)


@functools.lru_cache(maxsize=1)
def _cron_mains() -> tuple[str, ...]:
    return tuple(sorted(p.name for p in CRON.glob("*.py") if _has_main(p)))


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}


def _probe(script: Path, sandbox: Path) -> tuple[int | str, list[str], str]:
    """Run ``script PROBE_ARG`` in ``sandbox``; return (exit status, changed paths, stderr tail)."""
    vault = sandbox / "vault"
    (vault / "wiki").mkdir(parents=True)
    (sandbox / "home").mkdir()
    dead = "http://127.0.0.1:9"
    # No subprocess coverage: this is a behavioral probe, not coverage evidence. pytest-cov's
    # COV_CORE_* env would otherwise measure the import lines of scripts no test imports and turn
    # a file the suite never measured into a partial one (enrich_raw_years.py, MR pipeline 12899).
    inherited = {k: v for k, v in os.environ.items()
                 if not k.startswith("COV_CORE_") and k != "COVERAGE_PROCESS_START"}
    env = {**inherited, "WIKI_PATH": str(vault), "HOME": str(sandbox / "home"),
           "HERMES_HOME": str(sandbox / "hermes"), "PYTHONDONTWRITEBYTECODE": "1",
           "HTTP_PROXY": dead, "HTTPS_PROXY": dead, "http_proxy": dead, "https_proxy": dead,
           "PYTHONPATH": os.pathsep.join(str(REPO / d) for d in (".", "src", "scripts/cron"))}
    before = _snapshot(sandbox)
    try:
        proc = subprocess.run([sys.executable, str(script), PROBE_ARG], cwd=sandbox, env=env,
                              capture_output=True, text=True, timeout=60)
        rc: int | str = proc.returncode
        tail = proc.stderr.strip()[-400:]
    except subprocess.TimeoutExpired:
        rc, tail = "timeout", ""
    after = _snapshot(sandbox)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    return rc, changed, tail


def _verdict(rc: int | str, changed: list[str]) -> str | None:
    if changed:
        return f"wrote {changed[:5]} for an unknown argument"
    if rc != 2:
        return f"exit status {rc!r} for an unknown argument (expected argparse's 2)"
    return None


@pytest.mark.parametrize("name", [n for n in _cron_mains() if n not in NOT_YET_CONVERTED])
def test_cron_main_rejects_unknown_argument_without_side_effects(name, tmp_path):
    rc, changed, tail = _probe(CRON / name, tmp_path)
    problem = _verdict(rc, changed)
    assert problem is None, (
        f"scripts/cron/{name}: {problem}. Parse argv with argparse.parse_args BEFORE touching the "
        f"vault (okengine#1034). stderr: {tail}")


def test_allowlist_names_real_cron_mains():
    mains = set(_cron_mains())
    stale = sorted(set(NOT_YET_CONVERTED) - mains)
    assert not stale, f"NOT_YET_CONVERTED names scripts with no main(): {stale}; remove them"


def test_allowlist_only_shrinks():
    assert len(NOT_YET_CONVERTED) <= _ALLOWLIST_CEILING, (
        "a new cron script was allowlisted; make it parse its arguments instead (okengine#1034)")
    assert all(reason.strip() for reason in NOT_YET_CONVERTED.values())


def test_converted_scripts_leave_the_allowlist():
    converted = sorted(n for n in NOT_YET_CONVERTED
                       if re.search(r"\.parse_args\(", (CRON / n).read_text(encoding="utf-8")))
    assert not converted, (
        f"{converted} now call parse_args: remove them from NOT_YET_CONVERTED and lower "
        "_ALLOWLIST_CEILING so the probe covers them")


# ---- negative fixtures: the probe must catch the defect it exists for ----

def _fixture(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fixture_lane.py"
    script.write_text(body, encoding="utf-8")
    return script


def test_probe_flags_a_script_that_ignores_argv_and_writes(tmp_path):
    script = _fixture(tmp_path, (
        "import os, pathlib\n"
        "def main():\n"
        "    p = pathlib.Path(os.environ['WIKI_PATH']) / 'wiki' / 'dashboards' / 'x.md'\n"
        "    p.parent.mkdir(parents=True, exist_ok=True)\n"
        "    p.write_text('rewritten')\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"))
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    rc, changed, _ = _probe(script, sandbox)
    assert rc == 0
    assert "vault/wiki/dashboards/x.md" in changed
    assert _verdict(rc, changed).startswith("wrote")


def test_probe_flags_parse_known_args_that_swallows_the_argument(tmp_path):
    script = _fixture(tmp_path, (
        "import argparse\n"
        "def main():\n"
        "    argparse.ArgumentParser().parse_known_args()\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"))
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    rc, changed, _ = _probe(script, sandbox)
    assert (rc, changed) == (0, [])
    assert "exit status 0" in _verdict(rc, changed)


def test_probe_accepts_an_argparse_script(tmp_path):
    script = _fixture(tmp_path, (
        "import argparse\n"
        "def main():\n"
        "    argparse.ArgumentParser().parse_args()\n"
        "    raise RuntimeError('unreachable')\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"))
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    rc, changed, _ = _probe(script, sandbox)
    assert _verdict(rc, changed) is None
