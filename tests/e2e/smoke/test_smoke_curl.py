"""Smoke e2e — HTTP/content layer.

Asserts on the ACTUAL rendered output of the reader/cockpit/mcp surfaces served over the frozen
seeded vault (vault/). Each test reproduces a shape that regressed in production and would return
a green 200 from a liveness probe — only assertions on the rendered bytes catch them.

Run via smoke-e2e.sh (which stands the stack up, points the *_URL envs at the loopback ports, runs
this, then tears down). Standalone: bring the stack up with docker-compose.smoke.yml first. If the
stack is unreachable the module SKIPS (undetectable, not a vacuous pass) rather than failing.
"""
import importlib.util
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_RL = _REPO / "scripts" / "cron" / "render_lint.py"
_CL = _REPO / "scripts" / "cron" / "content_lint.py"
_VAULT = Path(__file__).resolve().parent / "vault"

READER = os.environ.get("SMOKE_READER_URL", "http://127.0.0.1:9880")
COCKPIT = os.environ.get("SMOKE_COCKPIT_URL", "http://127.0.0.1:9881")
MCP = os.environ.get("SMOKE_MCP_URL", "http://127.0.0.1:8880")
MCP_TOKEN = os.environ.get("SMOKE_MCP_TOKEN", "okengine-local")   # matches docker-compose.smoke.yml


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def _json(url, timeout=60):
    status, body = _get(url, timeout)
    return status, json.loads(body)


def _require_stack():
    try:
        _get(f"{READER}/healthz", timeout=8)
    except (urllib.error.URLError, OSError) as e:
        pytest.skip(f"smoke stack not reachable at {READER} ({e}) — run smoke-e2e.sh")


@pytest.fixture(autouse=True)
def _stack_up():
    _require_stack()


# ── liveness (the floor the old verifier already covered) ────────────────────

def test_reader_healthz():
    assert _get(f"{READER}/healthz")[0] == 200


def test_cockpit_dashboards_reachable():
    assert _json(f"{COCKPIT}/api/dashboards")[0] == 200


def _mcp_status(headers=None, timeout=8):
    req = urllib.request.Request(f"{MCP}/mcp", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code                         # a 4xx is still "the server answered"


def test_mcp_endpoint_answers():  # invariant-audit B7.6 (+ re-verify)
    """The smoke stack stands up the MCP container but nothing exercised it — and in fact the MCP
    EXITED at startup (default-token fail-closed) so the surface was never up. Now that compose lets
    it start, PROVE the real /mcp route, not just 'some HTTP server answers on the port': a bare 401
    comes from the auth wrapper for ANY path, so it's path-agnostic. Discriminate with the token —
    a misrouted port (e.g. pointing at the reader) would keep 401'ing a Bearer it doesn't know, or
    404 /mcp."""
    # 1. no token -> 401: the MCP auth wrapper is live (and the container is actually up)
    try:
        unauth = _mcp_status()
    except (urllib.error.URLError, OSError) as e:
        pytest.fail(f"MCP at {MCP} refused the connection — container down/EXITED "
                    f"(check the #50 fail-closed token guard in docker-compose.smoke.yml): {e}")
    assert unauth == 401, f"MCP /mcp without a token should be 401 (auth enforced), got {unauth}"
    # 2. WITH the smoke admin token -> the token is ACCEPTED (not 401) and the /mcp route EXISTS
    #    (not 404). Any other HTTP server on the port fails one of these.
    auth = _mcp_status(headers={"Authorization": f"Bearer {MCP_TOKEN}"})
    assert auth != 401, f"MCP rejected the smoke token on /mcp (got {auth}) — wrong token or not the MCP"
    assert auth != 404, f"/mcp route missing (got {auth}) — the port may point at a non-MCP server"


# ── render-surface regressions ───────────────────────────────────────────────

def test_actor_wikilinks_render_clean():
    """The recurring render class, both directions of the contract: a resolvable [[link]] (bare or
    backtick-wrapped) becomes an <a> with no `[[`/backtick residue, a genuine non-wikilink code span
    stays literal <code>, and the raw `<a class="wl">` builder markup never leaks as visible text."""
    _, d = _json(f"{READER}/api/page?path=entities/a/apt-smoke")
    html = d.get("html", "")
    assert "SMOKE_BODY_SENTINEL" in html, "body did not render"
    assert "<a" in html, "resolvable wikilink did not become a link"
    assert "&lt;a class=\"wl\"" not in html, "escaped wl markup leaked into the page"
    # a genuine (non-wikilink) inline-code span must survive as <code>, backticks->code, not stripped
    assert "<code>LITERAL_CODE_KEPT_x7</code>" in html, "genuine inline-code span was not preserved"
    # no wikilink may survive as literal [[…]] or leave backtick residue around it
    assert "[[entities/" not in html, "a wikilink leaked as literal [[…]] text"
    assert "`[[" not in html and "]]`" not in html, "backtick residue around a wikilink leaked"


def test_nested_dashboard_visible_in_grid():
    """M7: an un-curated NESTED dashboard must surface in the 'Other' catch-all, not vanish."""
    _, d = _json(f"{COCKPIT}/api/dashboards")
    paths = {it["path"] for g in d["groups"] for it in g["items"]}
    assert "dashboards/competitive/nested-smoke" in paths, f"nested dashboard missing: {sorted(paths)}"


def test_prediction_has_description():
    """The 'no description of the predictions' fix: a filed prediction renders a claim, not a bare
    count."""
    _, d = _json(f"{COCKPIT}/api/predictions")
    assert d["total"] >= 1, "no predictions loaded"
    claim = (d["rows"][0].get("claim") or "").strip()
    assert claim, "prediction rendered with an empty claim/description"
    assert "SMOKE_PREDICTION_SENTINEL" in claim


def test_weekly_deck_renders_pdf():
    """The 'deck pdf not found' 404 fix: the pdf-enabled stream renders a real PDF on demand."""
    status, body = _get(f"{COCKPIT}/api/stream.pdf?stream=deck&date=2026-01-01", timeout=120)
    assert status == 200, "deck endpoint did not return 200"
    assert body[:4] == b"%PDF", "deck endpoint did not return a PDF"
    assert len(body) > 1000, "deck PDF suspiciously small"


def test_multisource_embed_resolves_to_entities():
    """#16: a bare-basename embed present in two namespaces must resolve to the entities/ page."""
    _, d = _json(f"{READER}/api/page?path=dashboards/operator")
    html = d.get("html", "")
    assert "ETHERRAT_ENTITY_SENTINEL" in html, "embed did not resolve to the entities page"
    assert "ETHERRAT_INCIDENT_SENTINEL" not in html, "embed wrongly resolved to the incident page"


def _box_first_cells(tab: str, title: str) -> list[str]:
    """First-column text of each table row in the named box of /api/tab/<tab>."""
    _, d = _json(f"{COCKPIT}/api/tab/{tab}")
    box = next(b for b in d["boxes"] if b["title"] == title)
    cells = []
    for row in re.findall(r"<tr>.*?</tr>", box["html"]):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row)
        if tds:
            cells.append(re.sub(r"<[^>]+>", "", tds[0]).strip())
    return cells


def test_date_sorted_box_newest_first():
    """The Knowledge-gaps regression (2026-07-10): ISO dates aren't floatable, so a date-sorted box
    lives entirely in the sort's non-numeric bucket — `sort: {field: created, desc: true}` must show
    NEWEST first, with YAML date OBJECTS (unquoted) and strings (quoted) ordering consistently."""
    assert _box_first_cells("sorted", "Knowledge gaps") == ["Gap New", "Gap Mid", "Gap Old"]


def test_numeric_sorted_box_junk_ranks_last():
    """The Most-active regression (2026-07-10): a legacy page with a LIST where a count belongs must
    rank BELOW every real number in a desc numeric sort — never take the #1 slot."""
    cells = _box_first_cells("sorted", "Most active")
    assert cells[:2] == ["APT Smoke", "Zeta Actor"], cells      # 15, then 7
    assert cells[-1] == "Junk Count Actor", cells               # malformed value sinks to the bottom


def test_whole_vault_render_lint_is_clean():
    """Exercise the vault-wide render lint (scripts/cron/render_lint.py) end-to-end over the seeded vault:
    every page swept through the reader must render clean. This is the crawler that runs against real
    deployments; the seeded vault is its always-green fixture."""
    spec = importlib.util.spec_from_file_location("render_lint", _RL)
    rl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rl)
    paths = rl.enumerate_pages(READER)
    assert paths, "reader enumerated no pages"
    offenders = rl.crawl(READER, paths, workers=8)
    assert offenders == {}, f"render lint found defects in the seeded vault: {offenders}"


def test_seeded_vault_is_content_clean():
    """Exercise the content-quality lint (scripts/cron/content_lint.py) over the seeded vault: the frozen
    fixtures must be free of degeneration (repetition-loop word-salad). Guards the lint from
    false-positiving on normal prose and keeps the fixtures honest."""
    spec = importlib.util.spec_from_file_location("content_lint", _CL)
    cl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cl)
    offenders = cl.scan_vault(_VAULT / "wiki")
    assert offenders == {}, f"content lint flagged the seeded vault: {offenders}"
