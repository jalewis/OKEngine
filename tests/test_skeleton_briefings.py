"""Briefings-by-default. `briefing`/`briefings` are now CORE (engine-owned, okengine#90) — every
pack inherits them. The skeleton supplies the pack side: the brief cron (writes `wiki/briefings/`
`type: briefing`) and the "Briefs"/ANALYSIS rail_top_section — so briefs get their own reader
section and become the reader's default landing."""
import json
import re
import sys
from pathlib import Path

import yaml

SK = Path(__file__).resolve().parent.parent / "templates" / "pack" / "skeleton"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "cron"))
import schema_lib  # noqa: E402


def _schema():
    return yaml.safe_load(SK.joinpath("schema.yaml").read_text())


def test_briefings_is_core_inherited():
    # briefing TYPE + briefings NAMESPACE live in the engine core now — not declared per pack
    base = schema_lib.base_schema()
    assert "briefing" in base["types"]
    assert "briefings" in base["partitioning"]["namespaces"]
    assert "briefings" in base["tier"]["namespaces"]


def test_skeleton_rail_pins_briefs():
    # briefings must be pinned in the top rail (label is now "ANALYSIS" — the synthesized-outputs
    # cluster: briefings + trends + predictions + findings — since okengine#37).
    rt = _schema().get("rail_top_section") or {}
    assert "briefings" in (rt.get("namespaces") or [])


def test_skeleton_brief_cron_writes_briefings_with_briefing_type():
    # okengine#169: daily-brief is an engine-template lane now — the engine ships the
    # selector script + schedule (config/engine-crons.json stub), the pack ships ONLY
    # the prompt via crons/engine-template-prompts.json. The skeleton must therefore
    # carry no domain brief cron, and the prompt keeps the briefing-type contract.
    crons = json.loads(SK.joinpath("crons/domain-crons.json").read_text())
    jobs = crons["jobs"] if isinstance(crons, dict) else crons
    assert not [j for j in jobs if "brief" in j["name"].lower()], \
        "brief cron must not be a skeleton DOMAIN job (it's engine-template since #169)"
    p = json.loads(SK.joinpath("crons/engine-template-prompts.json").read_text())["daily-brief"]
    assert "wiki/briefings/" in p
    assert "type: briefing" in p
    assert "type: dashboard" not in p   # the old model — must be gone
    assert "updated:" in p              # fixes the blank-updated bug


def test_skeleton_does_not_re_own_core_briefing():
    # the skeleton owns only DOMAIN ids — briefing/briefings are core, not pack-owned (okengine#90)
    txt = re.sub(r"\{\{[^}]+\}\}", "x", SK.joinpath("pack.yaml").read_text())
    owns = yaml.safe_load(txt)["owns"]
    assert "briefing" not in owns["types"]
    assert "briefings" not in owns["namespaces"]
