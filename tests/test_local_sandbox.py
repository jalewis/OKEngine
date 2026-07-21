"""Contract tests for the one-command contributor sandbox (#73)."""

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MAKEFILE = REPO / "Makefile"
README = REPO / "tests" / "e2e" / "smoke" / "README.md"
COMPOSE = REPO / "tests" / "e2e" / "smoke" / "docker-compose.smoke.yml"


def test_sandbox_targets_reuse_the_verified_smoke_stack():
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "\nsandbox-start:" in makefile
    assert "smoke-e2e.sh --keep" in makefile
    assert "\nsandbox-stop:" in makefile
    assert "docker-compose.smoke.yml down -v --remove-orphans" in makefile
    assert "\nsandbox-reset:" in makefile
    assert "$(MAKE) sandbox-stop" in makefile
    assert "$(MAKE) sandbox-start" in makefile


def test_sandbox_is_loopback_only_and_fixture_vault_is_read_only():
    compose = COMPOSE.read_text(encoding="utf-8")

    assert "127.0.0.1:9880:9200" in compose
    assert "127.0.0.1:9881:9200" in compose
    assert "127.0.0.1:8880:8730" in compose
    assert compose.count("./vault:/vault:ro") >= 2
    assert "./vault:/opt/vault:ro" in compose


def test_sandbox_workflow_and_boundaries_are_documented():
    readme = README.read_text(encoding="utf-8")

    for command in ("make sandbox-start", "make sandbox-stop", "make sandbox-reset"):
        assert command in readme
    assert "not a miniature" in readme and "production agent" in readme
    assert "token `okengine-local`" in readme
