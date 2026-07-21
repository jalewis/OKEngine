#!/usr/bin/env python3
"""Content-quality lint — flag DEGENERATE agent generations in the vault.

The render lint and the smoke harness both pass a page full of word-salad, because it renders a clean
200. Degeneration is a CONTENT-quality failure, orthogonal to render: a model in a repetition loop
emits hundreds of filler words with no sentence structure. This reads the source markdown directly.

ONE signal, tuned HARD for precision over recall — a lint that cries wolf gets ignored:
  - long-unpunctuated-run  250+ words with no terminator (period/comma/;/:/newline) — repetition-loop
                           filler chaining. Commas terminate and wikilinks are stripped first, so a
                           long legitimate LIST (a MITRE mitigation page listing its techniques, a
                           malware page listing killed services) is not mistaken for filler.

Two false-positive shapes seen on a real multilingual CTI vault were designed out: a CJK-latin-fusion
signal was DROPPED (it can't tell code-switching degeneration `known漏洞` from legitimate Chinese CTI
— an APT name `熊猫Stealer`, an alias `XY助手`, a Chinese-language source); and long lists are excluded
as above. Validated: 0 false positives across five live vaults, still catches every 500-2000-word run.

Usage (on-demand):
    python scripts/cron/content_lint.py --vault /path/to/vault      # vault root (contains wiki/)
    python scripts/content_lint.py --vault ... --write-vault /path/to/vault   # write the dashboard
Exit code: 0 = clean (within --max-offenders), 1 = offenders over the threshold, 2 = usage.

As a cron: point --vault at WIKI_PATH's parent (or pass --wiki) and --write-vault; it writes
wiki/operational/content-lint.md and exits non-zero when a lane starts degrading.
"""
import argparse
import datetime
import os
import re
import sys
from pathlib import Path

# ── threshold (tuned for precision: only clear degeneration trips it) ─────────
# A pure line-length signal was tried and dropped: this vault writes one paragraph per line, so a
# coherent 2–3k-char paragraph (a verbose incident writeup) is normal and tripped it. The word-salad
# that IS degeneration runs 500+ words with no period; a verbose-but-coherent run-on tops out ~130.
# 250 cleanly separates them.
MAX_WORDS_NO_STOP = 250    # words between sentence terminators before it reads as filler chaining

_FM_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[[^\]]*\]\]")
# Commas terminate a run too: a long comma-separated LIST (MITRE techniques, killed
# services) is legitimate content, not filler. Real word-salad chains with SPACES and
# still trips this. A CJK-latin-fusion signal was tried and DROPPED: it cannot tell
# code-switching degeneration from legitimate Chinese CTI (an APT name, an alias, a
# Chinese-language source) on a multilingual vault.
_STOP = re.compile(r"[.!?;:\n,]")


def _body(text: str) -> str:
    """Prose body: drop YAML frontmatter and fenced code (literal CJK / long strings there are not
    degeneration)."""
    text = _FM_RE.sub("", text, count=1)
    return _WIKILINK_RE.sub(" ", _FENCE_RE.sub("\n", text))


def lint_text(path: str, text: str) -> list[str]:
    """Return the degeneration codes for one page (empty = clean)."""
    body = _body(text)
    worst = max((len(seg.split()) for seg in _STOP.split(body)), default=0)
    return ["long-unpunctuated-run"] if worst > MAX_WORDS_NO_STOP else []


# ── walk the vault ───────────────────────────────────────────────────────────

def scan_vault(wiki: Path) -> dict[str, list[str]]:
    offenders: dict[str, list[str]] = {}
    for p in wiki.rglob("*.md"):
        if p.name.startswith(("_", ".")):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        viol = lint_text(str(p), text)
        if viol:
            offenders[p.relative_to(wiki).as_posix()[:-3]] = viol
    return offenders


# ── report ───────────────────────────────────────────────────────────────────

def render_report(total: int, offenders: dict[str, list[str]], now: str) -> str:
    by_code: dict[str, int] = {}
    for viol in offenders.values():
        for c in viol:
            by_code[c] = by_code.get(c, 0) + 1
    n = len(offenders)
    L = ["---", "type: dashboard", 'title: "Content lint"', f"updated: {now}", "---", "",
         f"# Content lint — {now}", "",
         f"Scanned **{total:,}** pages for degeneration. **{n:,}** page(s) with a content-quality "
         f"defect (word-salad / code-switching bleed).", ""]
    if by_code:
        L += ["| Defect | Pages |", "|---|---|"]
        L += [f"| {c} | {k} |" for c, k in sorted(by_code.items(), key=lambda x: -x[1])]
        L += ["", "## Offenders", "", "| Page | Defects |", "|---|---|"]
        for pg in sorted(offenders)[:500]:
            L.append(f"| {pg} | {', '.join(offenders[pg])} |")
        if n > 500:
            L.append(f"| … | +{n - 500:,} more |")
    else:
        L.append("_No degeneration. Clean._")
    L.append("")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Content-quality lint over the vault source")
    ap.add_argument("--vault", default="", help="vault root (contains wiki/)")
    ap.add_argument("--wiki", default="", help="wiki dir directly (overrides --vault/WIKI_PATH)")
    ap.add_argument("--max-offenders", type=int, default=-1,
                    help="exit non-zero only when MORE than this many pages are degenerate. -1 "
                         "(default) = AUTO: max(10, 0.5%% of scanned pages). A report-only monitor "
                         "must not RED the whole fleet-health over the routine handful of degenerate "
                         "feed-ingests a large vault always carries — it should flag a degradation "
                         "SPIKE. The full list is always in wiki/operational/content-lint.md "
                         "regardless of the exit code. Env CONTENT_LINT_MAX_OFFENDERS overrides; an "
                         "explicit non-negative value wins over auto.")
    ap.add_argument("--write-vault", default="", help="vault root; writes wiki/operational/content-lint.md")
    ap.add_argument("--now", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    # CRON MODE: invoked arg-less by cron-plus (no_agent) with WIKI_PATH in the gateway env — resolve
    # the vault from it and WRITE the dashboard automatically, like deployment_validate/health_export.
    # On-demand (`make content-lint`, explicit --vault/--wiki) prints only unless --write-vault.
    write_vault = a.write_vault
    if a.wiki:
        wiki = Path(a.wiki)
    elif a.vault:
        wiki = Path(a.vault) / "wiki"
    elif os.environ.get("WIKI_PATH"):
        wp = Path(os.environ["WIKI_PATH"])
        wiki = wp if wp.name == "wiki" else wp / "wiki"
        write_vault = write_vault or str(wiki.parent)          # cron mode -> auto-write the dashboard
    else:
        print("content-lint: pass --vault or --wiki (or set WIKI_PATH)", file=sys.stderr)
        return 2
    if not wiki.is_dir():
        print(f"content-lint: no wiki dir at {wiki}", file=sys.stderr)
        return 2

    total = sum(1 for _ in wiki.rglob("*.md"))
    offenders = scan_vault(wiki)

    if write_vault:
        out = Path(write_vault) / "wiki" / "operational" / "content-lint.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_report(total, offenders, a.now or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")), encoding="utf-8")
        print(f"content-lint: wrote {out}")

    if a.json:
        import json
        print(json.dumps({"total": total, "offenders": offenders}, indent=2))
    else:
        by_code: dict[str, int] = {}
        for viol in offenders.values():
            for c in viol:
                by_code[c] = by_code.get(c, 0) + 1
        print(f"content-lint: scanned {total:,} pages, {len(offenders):,} degenerate "
              f"{dict(sorted(by_code.items(), key=lambda x: -x[1]))}")
        for pg in sorted(offenders)[:20]:
            print(f"  {pg}: {', '.join(offenders[pg])}")
        if len(offenders) > 20:
            print(f"  … +{len(offenders) - 20:,} more")
    # Effective alarm threshold: an env/explicit value wins; else AUTO-scale to the vault. The old
    # default 0 (alarm on ANY degenerate page) perpetually reddened fleet-health on a large ingesting
    # vault — a report-only quality monitor should signal a SPIKE, not the routine noise.
    env_max = os.environ.get("CONTENT_LINT_MAX_OFFENDERS", "").strip()
    if env_max:
        threshold = int(env_max)
    elif a.max_offenders >= 0:
        threshold = a.max_offenders
    else:
        threshold = max(10, -(-total * 5 // 1000))   # ceil(0.5% of scanned pages), floor 10
    over = len(offenders) > threshold
    if not a.json:
        print(f"content-lint: {len(offenders):,} degenerate / threshold {threshold:,} "
              f"({'OVER — content-degradation alarm' if over else 'within tolerance'})")
    return 1 if over else 0


if __name__ == "__main__":
    raise SystemExit(main())
