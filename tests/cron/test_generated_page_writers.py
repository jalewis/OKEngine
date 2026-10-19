"""okengine#933 / #920: every routed generated-page writer, run for real, keeps page identity.

Each writer runs as its cron does (a subprocess with WIKI_PATH at a disposable vault). For every
page it generates:

  * parity — the page carries exactly the id `backfill_ids` would stamp, so a backfill run after
    the writer stamps nothing;
  * preservation — an id already on the page survives the writer's next whole-file rewrite;
  * type (#920) — the page's `type` is a base-schema type (`config/base-schema.yaml`), so an
    engine report never lands as schema drift or gets aliased into a knowledge type.

Writers that need live services or operator configuration to produce a page are covered by the
static routing detector in `test_generated_page_routing.py`.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "cron"))
import generated_page as G  # noqa: E402

BASE_TYPES = set(yaml.safe_load((REPO / "config" / "base-schema.yaml").read_text())["types"])
#: The base-schema type for a generated view/report (okengine#920).
GENERATED_TYPE = "dashboard"


def _load_backfill():
    spec = importlib.util.spec_from_file_location("backfill_ids_933w", REPO / "scripts" / "backfill_ids.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BACKFILL = _load_backfill()

FIXTURES = {
    "wiki/entities/acme.md": '---\nid: "entities:acme"\ntype: entity\ntitle: Acme\nupdated: 2026-09-01\n---\n'
                             "Acme body [[sources/2026-09-01-x]]\n",
    "wiki/sources/2026-09-01-x.md": '---\nid: "sources:x"\ntype: source\ntitle: X\npublished: 2026-09-01\n'
                                    "---\nCreated [[entities/acme]] as a new entity.\n",
}


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for rel, text in FIXTURES.items():
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (vault / "schema.yaml").write_text("types:\n  entity: {required: [type]}\n", encoding="utf-8")
    for ns in ("dashboards", "operational", "concepts"):     # present on every deployed vault
        (vault / "wiki" / ns).mkdir()
    (vault / "raw").mkdir()
    (vault / "data").mkdir()
    (vault / "jobs.json").write_text(json.dumps({"jobs": []}), encoding="utf-8")
    return vault


def _event_schema(vault: Path) -> None:
    (vault / "schema.yaml").write_text("types:\n  entity: {required: [type]}\n"
                                       "event_types: [campaign]\nevent_date_field: first_seen\n",
                                       encoding="utf-8")
    page = vault / "wiki" / "campaigns" / "camp.md"
    page.parent.mkdir(parents=True)
    page.write_text('---\nid: "campaigns:camp"\ntype: campaign\ntitle: Camp\nfirst_seen: 2026-09\n---\nx\n',
                    encoding="utf-8")


def _prediction(vault: Path) -> None:
    page = vault / "wiki" / "predictions" / "p1.md"
    page.parent.mkdir(parents=True)
    page.write_text('---\nid: "predictions:p1"\ntype: prediction\ntitle: P1\nstatus: open\n'
                    "confidence: medium\nsubject: acme\nresolves_by: 2020-01-01\n---\nclaim\n",
                    encoding="utf-8")


def _completeness_rules(vault: Path) -> None:
    rules = vault / "config" / "completeness-rules.yaml"
    rules.parent.mkdir(parents=True)
    rules.write_text("rules:\n  - id: entity-owner\n    when: {type: entity}\n    expect: field\n"
                     "    field: owner\n    severity: medium\n", encoding="utf-8")


SETUP = {
    "extensions/okengine.events/build_event_ledger.py": _event_schema,
    "extensions/okengine.predictions/prediction_date_audit.py": _prediction,
    "extensions/okengine.predictions/prediction_schema_audit.py": _prediction,
    "extensions/okengine.completeness/completeness_audit.py": _completeness_rules,
}

#: Page types a first-party extension's own schema fragment declares for the pages it generates.
FRAGMENT_TYPES = {"extensions/okengine.completeness/completeness_audit.py": ("gap", "completeness")}

# script -> extra argv. Every entry produced at least one page in the disposable vault.
WRITERS = {
    "scripts/cron/collection_health.py": [],
    "scripts/cron/conformance_audit.py": [],
    "scripts/cron/content_lint.py": [],
    "scripts/cron/deployment_validate.py": [],
    "scripts/cron/detect_field_loss.py": [],
    "scripts/cron/fleet_health.py": [],
    "scripts/cron/generate_project_stats.py": [],
    "scripts/cron/grounding_audit.py": [],
    "scripts/cron/kb_health.py": [],
    "scripts/cron/lint_watcher.py": [],
    "scripts/cron/normalize_bare_name_links.py": [],
    "scripts/cron/observability_validate.py": [],
    "scripts/cron/operator_dashboard.py": [],
    "scripts/cron/page_quality_audit.py": [],
    "scripts/cron/reference_integrity.py": [],
    "scripts/cron/refresh_kb_dashboards.py": [],
    "scripts/cron/registry_coverage.py": [],
    "scripts/cron/review_queue.py": [],
    "scripts/cron/schema_drift_lint.py": [],
    "scripts/cron/select_source_staleness.py": [],
    "scripts/cron/source_backlink_drain.py": [],
    "scripts/cron/source_portfolio_watch.py": [],
    "scripts/cron/tier_refresh.py": [],
    "scripts/cron/wiki_health_audit.py": [],
    "extensions/okengine.assessments/adversarial_evidence.py": [],
    "scripts/cron/year_derivation_audit.py": [],
    "extensions/okengine.competitive-analytics/discover_competitors.py": [],
    "extensions/okengine.completeness/completeness_audit.py": [],
    "extensions/okengine.events/build_event_ledger.py": [],
    "extensions/okengine.predictions/prediction_date_audit.py": [],
    "extensions/okengine.predictions/prediction_schema_audit.py": [],
    "extensions/okengine.contradictions/select_contradictions.py": [],
    "extensions/okengine.events/event_scoring.py": [],
    "extensions/okengine.predictions/calibration_refresh.py": [],
    "extensions/okengine.predictions/confidence_recommender.py": [],
    "extensions/okengine.timeline/build_timeline.py": [],
    "extensions/okengine.viz/build_wardley_map.py": [],
}


def _entrypoint(script: str, vault: Path) -> Path:
    """Engine scripts run from the repository; an extension runs from the deployed layout
    (``<scripts>/<id>/<file>`` with the engine cron libs one level up), which is how
    deploy-cron-scripts.sh stages it and the only place its engine-lib import must resolve."""
    if not script.startswith("extensions/"):
        return REPO / script
    staged = vault.parent / "staged-scripts"
    ext_dir = Path(script).parent
    if not staged.is_dir():
        staged.mkdir()
        for lib in (REPO / "scripts" / "cron").glob("*.py"):
            shutil.copy(lib, staged)
        shutil.copytree(REPO / ext_dir, staged / ext_dir.name,
                        ignore=shutil.ignore_patterns("__pycache__"))
    return staged / ext_dir.name / Path(script).name


def _run(script: str, vault: Path) -> None:
    env = {**os.environ, "WIKI_PATH": str(vault), "HOME": str(vault), "HERMES_HOME": str(vault / "data"),
           "CRON_JOBS": str(vault / "jobs.json"), "METRICS_DIR": str(vault / "data" / "metrics"),
           "PYTHONPATH": os.pathsep.join([str(REPO / "src"), os.environ.get("PYTHONPATH", "")])}
    proc = subprocess.run([sys.executable, str(_entrypoint(script, vault)), *WRITERS[script]], cwd=vault, env=env,
                          capture_output=True, text=True, timeout=120)
    # Some lanes exit 1 to report findings; a crash (traceback) is never acceptable.
    assert "Traceback" not in proc.stderr, f"{script} crashed:\n{proc.stderr[-2000:]}"


def _inputs(vault: Path) -> set[str]:
    wiki = vault / "wiki"
    return {path.relative_to(wiki).as_posix() for path in wiki.rglob("*.md")}


def _generated(vault: Path, inputs: set[str]) -> dict[str, Path]:
    wiki = vault / "wiki"
    pages = {}
    for path in sorted(wiki.rglob("*.md")):
        rel = path.relative_to(wiki).as_posix()
        if rel in inputs or G.is_exempt(path):
            continue
        fm, _ = G.parse_frontmatter(path.read_text(encoding="utf-8"))
        if fm is not None:
            pages[rel] = path
    return pages


def _fm(path: Path) -> dict:
    return G.parse_frontmatter(path.read_text(encoding="utf-8"))[0]


def test_the_generated_type_is_declared_by_the_base_schema():
    assert GENERATED_TYPE in BASE_TYPES


@pytest.mark.parametrize("script", sorted(WRITERS))
def test_writer_keeps_identity_and_writes_a_base_type(tmp_path, script):
    vault = _vault(tmp_path)
    if script in SETUP:
        SETUP[script](vault)
    allowed_types = {GENERATED_TYPE}
    if script in FRAGMENT_TYPES:
        extra, fragment = FRAGMENT_TYPES[script]
        declared = yaml.safe_load((REPO / Path(script).parent / "schema" / f"{fragment}.schema.yaml")
                                  .read_text(encoding="utf-8"))["owns"]["types"]
        assert extra in declared, f"{extra!r} is not declared by {script}'s schema fragment"
        allowed_types.add(extra)
    inputs = _inputs(vault)
    _run(script, vault)
    pages = _generated(vault, inputs)
    assert pages, f"{script} generated no page in the disposable vault"

    for rel, path in pages.items():
        fm = _fm(path)
        assert G.page_id(fm), f"{script} wrote {rel} without an id"
        assert fm.get("type") in allowed_types, (
            f"{script} wrote {rel} with type {fm.get('type')!r}; an engine report must use the "
            f"base-schema generated-view type {GENERATED_TYPE!r}")
        if not G.page_id(fm).startswith("dashboard:"):   # writer-pinned ids predate #933
            expected, _ = BACKFILL.derive_for_page(vault, rel, {k: v for k, v in fm.items() if k != "id"})
            assert fm["id"] == expected, f"{script}: {rel} has {fm['id']!r}, backfill derives {expected!r}"
    assert BACKFILL.run(vault, apply=False)["stamped"] == 0, f"backfill would still stamp after {script}"

    sentinels = {}
    for rel, path in pages.items():
        text = path.read_text(encoding="utf-8")
        fm, match = G.parse_frontmatter(text)
        if G.page_id(fm).startswith("dashboard:"):
            continue
        sentinel = f"kept:{path.stem}"
        body = match.group(2).replace(f'id: "{fm["id"]}"\n', "", 1)
        path.write_text(match.group(1) + f'id: "{sentinel}"\n' + body + match.group(3)
                        + text[match.end():], encoding="utf-8")
        sentinels[rel] = sentinel
    _run(script, vault)
    for rel, sentinel in sentinels.items():
        assert _fm(pages[rel])["id"] == sentinel, f"{script} dropped the existing id of {rel} on rewrite"
