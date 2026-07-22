import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def _run(script, vault, manifest, extra=None):
    env = dict(os.environ, WIKI_PATH=str(vault), HERMES_HOME=str(vault / ".hermes"),
               OKENGINE_SELECTION_MANIFEST=str(manifest), OKENGINE_LANE_ID="lane",
               OKENGINE_CONTRACT_DIGEST="sha256:contract")
    env.update(extra or {})
    return subprocess.run([sys.executable, str(REPO / "scripts/cron" / script)],
                          env=env, text=True, capture_output=True)


def test_page_quality_selector_writes_exact_manifest(tmp_path):
    op = tmp_path / "wiki/operational"
    op.mkdir(parents=True)
    (op / "page-quality-queue.json").write_text(json.dumps([{
        "page": "entities/a/actor", "tier": "stub", "words": 4,
        "sections": 0, "sources": 0, "inbound": 2}]))
    src = tmp_path / "wiki/sources/report.md"
    src.parent.mkdir(parents=True)
    src.write_text("---\ntype: source\n---\nEvidence about [[entities/actor]] and activity.")
    manifest = tmp_path / "selection.json"
    run = _run("select_page_quality_enrich.py", tmp_path, manifest,
               {"PQ_ENRICH_BATCH": "1", "ENRICH_COOLDOWN_DAYS": "0"})
    assert run.returncode == 0, run.stderr
    assert json.loads(manifest.read_text())["selected"] == ["entities/a/actor"]


def test_schema_classifier_writes_exact_manifest(tmp_path):
    (tmp_path / "schema.yaml").write_text("types:\n  actor: {}\n")
    page = tmp_path / "wiki/entities/a/actor.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: entity\ncreated: 2020-01-01\n---\nAn actor page.\n")
    manifest = tmp_path / "selection.json"
    run = _run("select_schema_classify.py", tmp_path, manifest,
               {"SCHEMA_CLASSIFY_BATCH": "1", "SCHEMA_CLASSIFY_MIN_AGE": "0"})
    assert run.returncode == 0, run.stderr
    assert json.loads(manifest.read_text())["selected"] == ["entities/a/actor"]
