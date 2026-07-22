"""okengine.messaging-synthesis (#152) — generic vendor positioning synthesis, product anchor
as pack config.

Guards: the manifest shape, the deleak (NO product identity shipped), and that every one of the
4 selectors stays silent absent a product anchor — the universal "no fabricated vendor identity"
gate this extension exists to enforce.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extensions" / "okengine.messaging-synthesis"
SELECTORS = (
    "select_content_pegs.py",
    "select_positioning_battle_cards.py",
    "select_value_prop_refresh.py",
    "select_messaging_synthesis.py",
)


def _write(p: Path, body: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def _run(script: str, env: dict) -> str:
    return subprocess.run(
        [sys.executable, str(EXT / script)],
        env={**env, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
    ).stdout


def test_all_prompts_trust_the_digest():
    # a live run without this instruction burned its whole turn budget re-fetching competitor
    # pages the wake-gate had already printed, and never got to writing the actual output
    for name in ("content-pegs.md", "battle-cards.md", "value-prop-refresh.md",
                 "messaging-synthesis.md"):
        text = (EXT / "prompts" / name).read_text()
        assert "Trust the digest" in text, f"{name} missing the trust-the-digest instruction"


def test_manifest_shape():
    m = yaml.safe_load((EXT / "extension.yaml").read_text())
    assert m["id"] == "okengine.messaging-synthesis"
    assert m["trust"] == "in-gateway"
    assert "tier" not in m  # unsupported manifest keys must not survive as ignored decoration
    assert set(m["operations"]) == {
        "content-pegs", "positioning-battle-cards", "value-prop-gap-refresh", "messaging-synthesis",
    }
    assert "product_anchor_path" in m["config"]
    assert any("briefings/" in w for w in m["capabilities"]["write"])


def test_ships_no_product_identity():
    # the deleak: the extension must carry NO product-anchor / seed data files
    bad = [p for p in EXT.rglob("*")
           if p.suffix in (".yaml", ".yml") and p.name != "extension.yaml"]
    bad += list(EXT.rglob("*product-anchor*"))
    assert not bad, f"messaging-synthesis must ship no product identity, found: {bad}"


def test_all_selectors_silent_without_product_anchor(tmp_path):
    for script in SELECTORS:
        out = _run(script, {"WIKI_PATH": str(tmp_path),
                             "PRODUCT_ANCHOR_PATH": str(tmp_path / "nope.yaml")})
        assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": False}, script
        assert "no product configured" in out


def test_content_pegs_wakes_on_watchlist_relevant_source(tmp_path):
    anchor = tmp_path / "anchor.yaml"
    anchor.write_text(yaml.safe_dump({
        "product_name": "Acme Shield",
        "capability_pages": ["concepts/acme-suite"],
        "watchlist_segments": ["direct"],
    }))
    wl = tmp_path / "wl.yaml"
    wl.write_text(yaml.safe_dump({"segments": {"direct": {"competitors": ["acme-rival"]}}}))
    _write(tmp_path / "wiki/sources/2026/06/s1.md",
           "---\ntype: source\ntitle: rival move\npublished: '2026-06-28'\n---\n"
           "Acme Rival shipped a new feature.\n")
    out = _run("select_content_pegs.py", {
        "WIKI_PATH": str(tmp_path), "PRODUCT_ANCHOR_PATH": str(anchor), "WATCHLIST_PATH": str(wl),
        "CONTENT_PEGS_NOW": "2026-06-28",   # pin now to the source date so the lookback window is deterministic
    })
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": True}
    assert "acme-rival" in out.lower() or "sources/2026/06/s1" in out


def test_value_prop_refresh_wakes_on_min_new_signals(tmp_path):
    anchor = tmp_path / "anchor.yaml"
    anchor.write_text(yaml.safe_dump({
        "product_name": "Acme Shield",
        "capability_pages": [],
        "watchlist_segments": ["direct"],
    }))
    wl = tmp_path / "wl.yaml"
    wl.write_text(yaml.safe_dump({"segments": {"direct": {"competitors": ["acme-rival"]}}}))
    for i in range(3):
        _write(tmp_path / f"wiki/sources/2026/06/s{i}.md",
               f"---\ntype: source\ntitle: move {i}\npublished: '2026-06-2{i}'\n---\n"
               "Acme Rival announced something.\n")
    out = _run("select_value_prop_refresh.py", {
        "WIKI_PATH": str(tmp_path), "PRODUCT_ANCHOR_PATH": str(anchor), "WATCHLIST_PATH": str(wl),
        "VALUE_PROP_MIN_NEW_SIGNALS": "3",
    })
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": True}


def test_positioning_battle_cards_caps_batch_size(tmp_path):
    # a large watchlist's first-ever run finds every competitor "stale" (no card exists yet) —
    # must not ask one agent turn-budget to write all of them in a single session
    anchor = tmp_path / "anchor.yaml"
    anchor.write_text(yaml.safe_dump({
        "product_name": "Acme Shield", "capability_pages": [], "watchlist_segments": ["direct"],
    }))
    competitors = [f"acme-rival-{i}" for i in range(8)]
    wl = tmp_path / "wl.yaml"
    wl.write_text(yaml.safe_dump({"segments": {"direct": {"competitors": competitors}}}))
    for c in competitors:
        _write(tmp_path / f"wiki/entities/{c}.md",
               f"---\ntype: vendor\ntitle: {c}\nupdated: '2026-06-28'\n---\n- shipped something\n")
    out = _run("select_positioning_battle_cards.py", {
        "WIKI_PATH": str(tmp_path), "PRODUCT_ANCHOR_PATH": str(anchor), "WATCHLIST_PATH": str(wl),
        "POSITIONING_BATCH_SIZE": "3",
    })
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": True}
    assert out.count("-> write to") == 3, "batch cap should limit the digest to 3 cards, not 8"
    assert "8 card(s) need refresh" in out and "this run covers 3" in out


def test_positioning_battle_cards_same_day_edit_uses_mtime_tiebreak(tmp_path):  # okengine#326 [16]
    """A same-DATE competitor edit made after the card was written must still refresh the card. The
    old date-only string compare missed it; the mtime tiebreak (intra-day granularity) catches it."""
    anchor = tmp_path / "anchor.yaml"
    anchor.write_text(yaml.safe_dump({
        "product_name": "Acme Shield", "capability_pages": [], "watchlist_segments": ["direct"]}))
    wl = tmp_path / "wl.yaml"
    wl.write_text(yaml.safe_dump({"segments": {"direct": {"competitors": ["acme-rival"]}}}))
    comp = tmp_path / "wiki/entities/acme-rival.md"
    _write(comp, "---\ntype: vendor\ntitle: acme-rival\nupdated: '2026-06-28'\n---\n- shipped v1\n")
    card = tmp_path / "wiki/briefings/positioning-direct-acme-rival.md"
    _write(card, "---\ntype: briefing\ntitle: card\nupdated: '2026-06-28'\n---\nold card\n")
    env = {"WIKI_PATH": str(tmp_path), "PRODUCT_ANCHOR_PATH": str(anchor), "WATCHLIST_PATH": str(wl)}
    base = 1_700_000_000

    # same date, competitor page modified AFTER the card -> stale (would be MISSED by a date-only compare)
    os.utime(card, (base, base)); os.utime(comp, (base + 100, base + 100))
    out = _run("select_positioning_battle_cards.py", env)
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": True}, out
    assert "-> write to" in out

    # same date, card written AFTER the competitor page -> NOT stale
    os.utime(card, (base + 200, base + 200)); os.utime(comp, (base + 100, base + 100))
    out = _run("select_positioning_battle_cards.py", env)
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": False}, out


def test_messaging_synthesis_silent_when_no_upstream_deltas(tmp_path):
    anchor = tmp_path / "anchor.yaml"
    anchor.write_text(yaml.safe_dump({"product_name": "Acme Shield", "capability_pages": []}))
    out = _run("select_messaging_synthesis.py", {
        "WIKI_PATH": str(tmp_path), "PRODUCT_ANCHOR_PATH": str(anchor),
    })
    # no briefings/ dir at all -> genuinely nothing to synthesize, must stay silent, not crash
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": False}


def test_messaging_synthesis_wakes_on_new_content_peg(tmp_path):
    anchor = tmp_path / "anchor.yaml"
    anchor.write_text(yaml.safe_dump({"product_name": "Acme Shield", "capability_pages": []}))
    _write(tmp_path / "wiki/briefings/content-pegs-2026-06-28.md",
           "---\ntype: marketing-pulse\npublished: '2026-06-28'\nupdated: '2026-06-28'\n---\n"
           "# pegs\n")
    out = _run("select_messaging_synthesis.py", {
        "WIKI_PATH": str(tmp_path), "PRODUCT_ANCHOR_PATH": str(anchor),
    })
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": True}


def _anchor(tmp_path):
    a = tmp_path / "anchor.yaml"
    a.write_text(yaml.safe_dump({"product_name": "Acme Shield",
                                 "capability_pages": ["concepts/acme-suite"]}))
    return a


def test_messaging_synthesis_daily_floor_wakes_with_no_delta(tmp_path):
    """okengine#177 principle: a reader-facing daily brief must run daily. With no
    upstream delta AND no brief yet today, the gate must WAKE (steady-state brief) —
    so a missing brief can only mean a broken pipeline, not a skipped gate."""
    a = _anchor(tmp_path)
    (tmp_path / "wiki" / "briefings").mkdir(parents=True)
    out = _run("select_messaging_synthesis.py",
               {"WIKI_PATH": str(tmp_path), "PRODUCT_ANCHOR_PATH": str(a)})
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": True}, out
    assert "STEADY-STATE" in out


def test_daily_floor_silent_once_todays_brief_exists(tmp_path):
    """No double-run: once today's brief is written, no-delta stays silent."""
    a = _anchor(tmp_path)
    import datetime
    # can't call date.today() deterministically here — write a brief for whatever 'today' is
    from datetime import date
    (tmp_path / "wiki" / "briefings").mkdir(parents=True)
    (tmp_path / "wiki" / "briefings" / f"messaging-brief-{date.today().isoformat()}.md").write_text(
        "---\ntype: messaging-brief\n---\nalready done\n")
    out = _run("select_messaging_synthesis.py",
               {"WIKI_PATH": str(tmp_path), "PRODUCT_ANCHOR_PATH": str(a)})
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": False}, out


def test_drift_only_opt_out_restores_pure_gating(tmp_path):
    """MESSAGING_DRIFT_ONLY=1 restores the old behavior: no delta -> silent."""
    a = _anchor(tmp_path)
    (tmp_path / "wiki" / "briefings").mkdir(parents=True)
    out = _run("select_messaging_synthesis.py",
               {"WIKI_PATH": str(tmp_path), "PRODUCT_ANCHOR_PATH": str(a),
                "MESSAGING_DRIFT_ONLY": "1"})
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": False}, out


def test_messaging_brief_prompt_never_instructs_silent():
    """okengine#177: the messaging-synthesis meta-brief has a DAILY FLOOR — if the
    selector wakes it, it MUST write a brief. The prompt must not tell the agent to
    respond [SILENT] (the second gate that hid behind the first). The underlying
    artifact prompts (content-pegs etc.) may still gate — only the reader-facing
    daily brief is floored."""
    prompt = (EXT / "prompts" / "messaging-synthesis.md").read_text()
    # the old DIRECTIVE to skip must be gone...
    assert "respond `[SILENT]` instead" not in prompt, \
        "prompt still directs a silent skip on no-change days (breaks the daily floor)"
    # ...and the explicit prohibition must be present
    assert "NEVER respond `[SILENT]`" in prompt


def test_messaging_synthesis_detects_same_day_upstream_delta(tmp_path):  # invariant-audit #18
    """An upstream artifact written LATER the same day as the last brief (the routine @morning stagger)
    compared equal-DATE and was never detected. mtime has intra-day granularity, so 'newer than the
    last brief' is expressible: with today's brief present (daily floor satisfied), a newer same-day
    content-peg must still WAKE via the delta path."""
    import os
    import datetime
    today = datetime.date.today().isoformat()
    a = _anchor(tmp_path)
    brief = tmp_path / f"wiki/briefings/messaging-brief-{today}.md"
    _write(brief, f"---\ntype: marketing-brief\npublished: '{today}'\nupdated: '{today}'\n---\n# brief\n")
    peg = tmp_path / f"wiki/briefings/content-pegs-{today}.md"
    _write(peg, f"---\ntype: marketing-pulse\npublished: '{today}'\nupdated: '{today}'\n---\n# pegs\n")
    os.utime(brief, (1000, 1000))          # the brief
    os.utime(peg, (2000, 2000))            # the peg, written AFTER it (same DATE, later mtime)
    out = _run("select_messaging_synthesis.py", {
        "WIKI_PATH": str(tmp_path), "PRODUCT_ANCHOR_PATH": str(a)})
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": True}, out
