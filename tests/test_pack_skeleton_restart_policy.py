"""okengine — the pack skeleton must not re-introduce the compose restart-policy override.

In non-swarm Compose, `deploy.restart_policy.condition` OVERRIDES the top-level
`restart:` and silently forces on-failure, which does not recover a clean stop. That
cost a 13-hour outage in June 2026.

Every live deployment had the block removed after that incident. The SKELETON kept it —
so every pack created from the template since then re-introduced the bug, and nothing
noticed because the running fleet looked correct. This pins the template to the same
contract the fleet already satisfies.

The gateway cap is pinned here too: 3072M was set on the premise the gateway "idles
~450M", and on 2026-08-15 every gateway in the fleet was measured PEAKING at its 3072M
limit while cgroups OOM-killed lane processes inside containers reporting healthy.
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
SKELETON = REPO / "templates" / "pack" / "skeleton" / "docker-compose.yml"


def rendered() -> dict:
    """The skeleton carries {{PLACEHOLDER}} tokens, so it is not YAML until substituted."""
    text = re.sub(r"\{\{[A-Z_]+\}\}", "placeholder", SKELETON.read_text(encoding="utf-8"))
    return yaml.safe_load(text)


def services() -> dict:
    return rendered().get("services") or {}


def test_the_skeleton_renders_to_valid_compose():
    assert services(), "skeleton produced no services"


def test_no_service_declares_deploy_restart_policy():
    """The `condition:` silently overrides `restart:` and forces on-failure."""
    offenders = [name for name, svc in services().items()
                 if (svc.get("deploy") or {}).get("restart_policy") is not None]
    assert offenders == [], (
        f"deploy.restart_policy present on {offenders} — in non-swarm Compose this "
        "overrides `restart: unless-stopped` and does not recover a clean stop "
        "(June 2026 13-hour outage)"
    )


def test_the_raw_text_carries_no_restart_policy_key():
    """Belt and braces: a commented-out block is fine, a live key is not."""
    keys = re.findall(r"^\s*restart_policy:", SKELETON.read_text(encoding="utf-8"), re.M)
    assert keys == []


def test_every_service_is_unless_stopped():
    wrong = {name: svc.get("restart") for name, svc in services().items()
             if svc.get("restart") != "unless-stopped"}
    assert wrong == {}, f"restart policy must be unless-stopped: {wrong}"


def test_every_service_keeps_an_explicit_resource_limit():
    """Removing restart_policy must not take resources.limits with it — unbounded use
    cascades into exactly the host-wide OOM this file documents."""
    missing = [name for name, svc in services().items()
               if not ((svc.get("deploy") or {}).get("resources") or {}).get("limits")]
    assert missing == [], f"service(s) with no resource limit: {missing}"


def test_the_gateway_cap_clears_the_measured_fleet_peak():
    """Measured 2026-08-15: every gateway peaked at 3.0-3.1G against a 3072M cap."""
    limits = (services()["gateway"]["deploy"]["resources"]["limits"])
    megabytes = int(str(limits["memory"]).rstrip("M"))
    assert megabytes >= 6144, (
        f"gateway limit {megabytes}M — the whole fleet was measured peaking at its "
        "3072M cap with cgroup OOM kills inside 'healthy' containers"
    )
