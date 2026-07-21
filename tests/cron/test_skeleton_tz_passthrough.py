"""Detector for okengine#301 — the deployment compose skeleton must pass TZ to every service.

The deployment timezone (`$TZ`) governs the reader/cockpit CLOCKS and the dates cron scripts stamp
onto content. A container only receives `$TZ` if its compose service declares `TZ=${TZ:-UTC}` in its
environment. When the skeleton omits that for a service, every deployment generated from it ships a
container silently pinned to UTC even though `.env` sets a real zone — the live drift that shipped to
cyber-market/competitive/ai-research readers (their compose predated the reader's TZ passthrough).

This locks the ROOT fix: every service the skeleton defines must carry the TZ passthrough, so a newly
generated deployment can never regress the class. (The complementary LIVE catch for already-drifted
deployments is post_deploy_verify.sh's tz check.)
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

SKELETON = Path(__file__).resolve().parents[2] / "templates" / "pack" / "skeleton" / "docker-compose.yml"
pytestmark = pytest.mark.skipif(not SKELETON.is_file(), reason="skeleton compose absent")


def _services() -> dict:
    # the skeleton carries {{PACK}}-style generator placeholders that aren't valid YAML scalars in
    # every position; neutralize them so we can parse the structure (we only inspect environment).
    raw = re.sub(r"\{\{[^}]+\}\}", "x", SKELETON.read_text(encoding="utf-8"))
    return (yaml.safe_load(raw) or {}).get("services") or {}


def test_every_skeleton_service_receives_TZ():
    missing = []
    for svc, spec in _services().items():
        env = (spec or {}).get("environment") or []
        flat = env if isinstance(env, list) else [f"{k}={v}" for k, v in env.items()]
        if not any(str(e).split("=", 1)[0].strip() == "TZ" for e in flat):
            missing.append(svc)
    assert not missing, (
        "compose skeleton service(s) do not pass TZ to the container — deployments generated from it "
        "will ignore the .env timezone (clocks + content dates default to UTC; okengine#301). Add "
        "`- TZ=${TZ:-UTC}` to each: " + ", ".join(missing)
    )
