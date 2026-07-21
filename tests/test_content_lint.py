"""Unit tests for the content-quality lint (scripts/cron/content_lint.py).

Pins the pure predicate — lint_text — that decides whether a page is degenerate. It must catch the
real class (repetition-loop word-salad) and must NOT false-positive on legitimate content: a long
comma-separated LIST (MITRE techniques, killed services), a long wikilink list, a coherent verbose
paragraph, or Chinese CTI content (an APT name / alias / Chinese-language source). The CJK-latin-
fusion signal was dropped precisely because it can't tell that Chinese content from degeneration.
"""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "scripts" / "cron" / "content_lint.py"


def _load():
    spec = importlib.util.spec_from_file_location("content_lint", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


cl = _load()
FM = "---\ntype: concept\ntitle: X\n---\n"
SALAD = " ".join(f"term{i}" for i in range(400))          # 400 space-separated words, no terminator


# ── the word-salad run is caught ─────────────────────────────────────────────

def test_word_salad_run_is_flagged():
    assert "long-unpunctuated-run" in cl.lint_text("x", FM + "# X\n\n" + SALAD + ".\n")


def test_verbose_runon_below_threshold_is_not_flagged():
    """A 130-word run-on is poor writing but not clearly degeneration — precision over recall."""
    run = " ".join(f"term{i}" for i in range(130))
    assert cl.lint_text("x", FM + "# X\n\n" + run + ".\n") == []


# ── legitimate content must NOT be flagged (the cyber-market false positives) ─

def test_clean_prose_is_clean():
    assert cl.lint_text("x", FM + "# X\n\nA normal concept page. It has sentences. They end.\n") == []


def test_long_comma_separated_list_is_not_flagged():
    """A MITRE mitigation page lists every technique it addresses / a malware page lists killed
    services — 300 comma-separated items, legitimate content, NOT filler chaining."""
    lst = ", ".join(f"Some Service Name {i}" for i in range(300))
    assert cl.lint_text("x", FM + "# Mitigation\n\nApplies to: " + lst + ".\n") == []


def test_long_wikilink_list_is_not_flagged():
    links = " ".join(f"[[entities/t/technique-{i}|Technique {i}]]" for i in range(300))
    assert cl.lint_text("x", FM + "# Mitigation\n\n" + links + "\n") == []


def test_chinese_cti_content_is_not_flagged():
    """A Chinese APT name, a fused alias, and a Chinese-language sentence are all legitimate CTI —
    the dropped CJK-fusion signal used to flag these."""
    body = (FM + "# Actor\n\nAliases: XY助手, 熊猫Stealer. Derives from Chinese \"mac注入\".\n"
            "东南亚新APT组织持续活跃，暗石组织借助多阶段投递链传播。\n")
    assert cl.lint_text("x", body) == []


def test_cjk_signal_is_gone():
    """The old code-switching signature must no longer flag anything — it was too noisy on a
    multilingual vault to be usable."""
    assert cl.lint_text("x", FM + "The known漏洞 was disclosed.\n") == []


# ── scan + cron mode ─────────────────────────────────────────────────────────

def test_cron_mode_writes_dashboard_from_wiki_path(tmp_path, monkeypatch):
    """Registered as a no_agent cron, it's invoked ARG-LESS with WIKI_PATH in the gateway env — it
    must resolve the vault and WRITE wiki/operational/content-lint.md automatically (like the other
    audit lanes), not just print."""
    wiki = tmp_path / "wiki" / "concepts" / "v"
    wiki.mkdir(parents=True)
    (wiki / "bad.md").write_text(FM + "# Bad\n\n" + SALAD + ".\n", encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    rc = cl.main([])                                         # arg-less, as cron-plus invokes it
    out = tmp_path / "wiki" / "operational" / "content-lint.md"
    assert out.is_file(), "cron mode did not write the dashboard"
    assert "long-unpunctuated-run" in out.read_text(encoding="utf-8")
    assert rc == 0                                           # 1 degenerate is WITHIN the auto tolerance
    # (max(10, 0.5%)) — a report-only monitor must not RED fleet-health on a single page; the strict
    # opt-in still alarms:
    assert cl.main([]) == 0 and cl.main(["--wiki", str(tmp_path / "wiki"), "--max-offenders", "0"]) == 1


def test_scan_vault_and_report(tmp_path):
    wiki = tmp_path / "wiki" / "concepts" / "v"
    wiki.mkdir(parents=True)
    (wiki / "clean.md").write_text(FM + "# Clean\n\nFine prose. Ends well.\n", encoding="utf-8")
    (wiki / "bad.md").write_text(FM + "# Bad\n\n" + SALAD + ".\n", encoding="utf-8")
    offenders = cl.scan_vault(tmp_path / "wiki")
    assert set(offenders) == {"concepts/v/bad"}
    rep = cl.render_report(2, offenders, "2026-01-01T00:00:00Z")
    assert "long-unpunctuated-run" in rep and "concepts/v/bad" in rep
    assert "Clean" in cl.render_report(2, {}, "2026-01-01T00:00:00Z")


# ── exit-code threshold: a report-only monitor must not RED the fleet on routine noise ─────────

def _vault(tmp_path, n_degenerate, n_clean):
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    for i in range(n_degenerate):
        (wiki / f"deg{i}.md").write_text(FM + f"# D{i}\n\n" + SALAD + ".\n", encoding="utf-8")
    for i in range(n_clean):
        (wiki / f"ok{i}.md").write_text(FM + f"# OK{i}\n\nA short coherent page. It ends.\n", encoding="utf-8")
    return wiki


def test_auto_threshold_tolerates_a_routine_handful(tmp_path):  # invariant-audit follow-up
    """The default (--max-offenders -1 = auto) must NOT exit non-zero on a handful of degenerate
    pages — else content-lint perpetually reds fleet-health on any large ingesting vault. Auto floor
    is 10, so 5 degenerate is within tolerance -> exit 0."""
    wiki = _vault(tmp_path, n_degenerate=5, n_clean=20)
    assert cl.main(["--wiki", str(wiki), "--max-offenders", "-1"]) == 0


def test_explicit_and_env_thresholds_still_alarm(tmp_path, monkeypatch):
    """An explicit non-negative --max-offenders and the CONTENT_LINT_MAX_OFFENDERS env both override
    the auto default and still fire on a spike over that bar."""
    wiki = _vault(tmp_path, n_degenerate=5, n_clean=5)
    monkeypatch.delenv("CONTENT_LINT_MAX_OFFENDERS", raising=False)
    assert cl.main(["--wiki", str(wiki), "--max-offenders", "0"]) == 1     # strict opt-in
    assert cl.main(["--wiki", str(wiki), "--max-offenders", "10"]) == 0    # 5 <= 10
    monkeypatch.setenv("CONTENT_LINT_MAX_OFFENDERS", "3")
    assert cl.main(["--wiki", str(wiki), "--max-offenders", "-1"]) == 1    # env 3 < 5 -> alarm
    monkeypatch.setenv("CONTENT_LINT_MAX_OFFENDERS", "10")
    assert cl.main(["--wiki", str(wiki), "--max-offenders", "-1"]) == 0    # env 10 >= 5 -> clear
