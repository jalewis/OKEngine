"""okengine#1027 follow-up: the release smoke stack's review-write service runs write_server.py
against the fixture vault. The write server refuses to start without a pack principal, so the
fixture must declare its pack exactly as a deployment vault does. Without it, every release lane
(e2e, resilience, performance) failed on main with review-write never becoming ready."""
from pathlib import Path

import yaml

from okengine.pack_principal import startup_refusal

REPO = Path(__file__).resolve().parent.parent
VAULT = REPO / "tests" / "e2e" / "smoke" / "vault"
COMPOSE = REPO / "tests" / "e2e" / "smoke" / "docker-compose.smoke.yml"


def test_the_smoke_fixture_vault_gives_its_write_server_a_pack_principal():
    service = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["review-write"]
    env = dict(entry.split("=", 1) for entry in service["environment"])
    assert "OKENGINE_PACK" not in env, "the principal comes from pack.yaml, not the environment"
    assert startup_refusal(VAULT, env) is None, startup_refusal(VAULT, env)
