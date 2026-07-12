"""invariant-audit v0.11.5 batch-6 — static gates for extension/app/compose/doc findings."""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_skeleton_mcp_forwards_index_refresh_hours():  # invariant-audit #35
    """OKENGINE_MCP_INDEX_REFRESH_HOURS is documented + read by server.py, but the mcp service's
    explicit environment: list (no env_file) didn't forward it — the documented `=0` disable never
    reached the container. It must be listed."""
    compose = (REPO / "templates" / "pack" / "skeleton" / "docker-compose.yml").read_text()
    assert "OKENGINE_MCP_INDEX_REFRESH_HOURS=${OKENGINE_MCP_INDEX_REFRESH_HOURS:-6}" in compose


def test_cockpit_today_prefix_uses_utc_not_local():  # invariant-audit #61
    """published/created are UTC ISO timestamps; the today_prefix filter must bucket by UTC today, not
    the container's LOCAL date (drops rows between UTC midnight and local midnight on a non-UTC host)."""
    app = (REPO / "okengine-cockpit" / "app.py").read_text()
    i = app.index("today_prefix")
    block = app[i:i + 700]
    assert "timezone.utc" in block, "today_prefix must compute today in UTC"
    assert "datetime.date.today().isoformat()" not in block, "still uses the LOCAL date"


def test_reader_chat_gated_on_budget_marker():  # invariant-audit #37
    """The reader /api/chat must refuse to relay while budget_guard's vault pause-marker exists, so
    chat spend honors the same budget trip that pauses the crons."""
    app = (REPO / "okengine-reader" / "app.py").read_text()
    assert "_budget_tripped" in app and "budget-paused" in app
    chat = app[app.index("async def api_chat"):app.index("async def api_chat") + 900]
    assert "_budget_tripped()" in chat, "/api/chat must check the budget trip before relaying"


def test_manual_bringup_sequences_use_build():  # invariant-audit #45
    """The documented manual bring-up sequences must use `up -d --build`, or a manual bring-up on an
    engine update ships stale reader/mcp/cockpit images (plain up -d only builds when ABSENT)."""
    for rel in ("templates/pack/skeleton/docker-compose.yml", "INSTALL.md", "docs/deploy-a-new-domain.md"):
        text = (REPO / rel).read_text()
        # the 'builds reader+mcp / runs all three' bring-up line must carry --build
        for line in text.splitlines():
            if "docker compose up -d" in line and ("reader" in line.lower() or "all three" in line.lower()):
                assert "--build" in line, f"{rel}: manual bring-up missing --build: {line.strip()}"
