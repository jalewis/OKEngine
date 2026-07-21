#!/usr/bin/env python3
"""completeness_audit.py — okengine.completeness: evaluate pack-declared completeness
rules over the corpus and maintain the gap queue. no_agent, deterministic, idempotent.

The boundary split (relevance-gate pattern): this script is domain-agnostic machinery;
ALL domain judgment lives in the pack's rules file (config/completeness-rules.yaml):

    rules:
      - id: vendor-needs-exposure-page          # stable — keys gap identity
        title: "Vendor without an exposure page"
        when: {type: vendor}                    # selector: pages of this type
        # optionally narrow: when: {type: risk, has_field: accepted}
        expect: companion                       # field | link | companion | freshness
        companion: "exposure/{slug}"            # page that must exist (wiki-relative, {slug} substituted)
        severity: high                          # low | medium | high
        resolution_hint: "Create the exposure decision page for this vendor."

      - id: ttp-needs-detection
        when: {type: ttp}
        expect: link                            # body must wikilink >=1 page matching target
        link: {prefix: "detections/"}           #   by path prefix ...
        # link: {type: detection}               #   ... or by target page type
        severity: high

      - id: risk-owner
        when: {type: risk}
        expect: field                           # frontmatter field present and non-empty
        field: owner
        severity: medium

      - id: assumption-freshness
        when: {type: assumption}
        expect: freshness                       # date field older than max_age_days -> gap
      - id: prediction-needs-refutation-criteria
        when: {type: prediction}
        expect: section                         # body H2 containing `section` w/ >=min_chars content
        section: What would refute this        # gradeability gate, okengine#214
        min_chars: 20
        severity: medium
        field: last_reviewed
        max_age_days: 90
        severity: medium

Gap lifecycle (pages in gaps/, type `gap`, id = <rule>--<subject-slug>):
  open       -> created/refreshed while the expectation is unmet (last_seen bumps)
  resolved   -> auto: the expectation is now satisfied (page kept — audit trail)
  dismissed  -> operator sets status: dismissed + dismiss_reason; NEVER reopened by the
                lane. Dismissals feed per-rule precision on the dashboard: a rule whose
                gaps are mostly dismissed is a rule to fix or retire.

Env: WIKI_PATH (/opt/vault) · COMPLETENESS_RULES (config/completeness-rules.yaml) ·
     COMPLETENESS_MAX_PER_RULE (200)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
RULES_FILE = os.environ.get(
    "COMPLETENESS_RULES",
    os.environ.get("OKENGINE_COMPLETENESS_RULES_FILE", "config/completeness-rules.yaml"),
)
MAX_PER_RULE = int(os.environ.get(
    "COMPLETENESS_MAX_PER_RULE",
    os.environ.get("OKENGINE_COMPLETENESS_MAX_GAPS_PER_RULE", "200"),
))
GAPS = WIKI / "gaps"
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)
_LINK = re.compile(r"\[\[\s*([A-Za-z0-9._/-]+?)\s*(?:[|#\]])")
SEVERITIES = ("high", "medium", "low")


def _split(p: Path):
    try:
        t = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    m = _FM.match(t)
    if not m:
        return {}, t
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        fm = {}
    return (fm if isinstance(fm, dict) else {}), t[m.end():]


def load_rules() -> list[dict] | None:
    f = VAULT / RULES_FILE
    if not f.is_file():
        return None
    try:
        d = yaml.safe_load(f.read_text(encoding="utf-8", errors="replace")) or {}
    except yaml.YAMLError as e:
        print(f"completeness-audit: rules file unparseable ({e}) — refusing to run")
        return None
    rules = d.get("rules")
    if not isinstance(rules, list) or not rules:
        return None
    ok = []
    for r in rules:
        if not (isinstance(r, dict) and r.get("id") and isinstance(r.get("when"), dict)
                and r.get("expect") in ("field", "link", "companion", "freshness", "section")):
            print(f"completeness-audit: skipping malformed rule: {r!r:.120}")
            continue
        r.setdefault("severity", "medium")
        ok.append(r)
    return ok or None


def _selected(fm: dict, when: dict) -> bool:
    if str(fm.get("type") or "").strip() != str(when.get("type") or ""):
        return False
    hf = when.get("has_field")
    if hf and not fm.get(hf):
        return False
    return True


def _page_index():
    """One corpus pass: (rel-key, slug, fm, body-links, path) for every page + lookup maps."""
    pages = []
    by_type: dict[str, set] = {}
    keys: set[str] = set()
    for p in WIKI.rglob("*.md"):
        if p.name.startswith(("_", ".")) or p.name.startswith("INDEX"):
            continue
        rel = p.relative_to(WIKI).as_posix()[:-3]
        fm, body = _split(p)
        keys.add(rel)
        t = str(fm.get("type") or "").strip()
        if t:
            by_type.setdefault(t, set()).add(rel)
        pages.append((rel, p.stem.lower(), fm, body))
    return pages, keys, by_type


def _unmet(rule: dict, rel: str, slug: str, fm: dict, body: str,
           keys: set, by_type: dict) -> str | None:
    """The unmet-expectation description, or None when satisfied."""
    kind = rule["expect"]
    if kind == "field":
        f = rule.get("field") or ""
        v = fm.get(f)
        return None if v not in (None, "", [], {}) else f"frontmatter field `{f}` is missing/empty"
    if kind == "companion":
        pat = str(rule.get("companion") or "").format(slug=slug).strip("/")
        return None if pat in keys else f"companion page `{pat}` does not exist"
    if kind == "link":
        spec = rule.get("link") or {}
        targets = _LINK.findall(body)
        if spec.get("prefix"):
            pref = str(spec["prefix"]).strip("/")
            hit = any(t.strip("/").startswith(pref) for t in targets)
            return None if hit else f"no wikilink to a `{pref}/` page"
        if spec.get("type"):
            tset = by_type.get(str(spec["type"]), set())
            tstems = {k.rsplit("/", 1)[-1] for k in tset}
            hit = any(t.strip("/") in tset or t.rsplit("/", 1)[-1] in tstems for t in targets)
            return None if hit else f"no wikilink to a `type: {spec['type']}` page"
        return None
    if kind == "section":
        # gradeability gate (okengine#214): a resolvable proposition must carry a substantive
        # body section (e.g. "What would refute this") — 31% of cyber-market's expired
        # predictions were ungradeable because nothing machine-checkable required criteria.
        want = str(rule.get("section") or "").strip().lower()
        min_chars = int(rule.get("min_chars") or 20)
        if not want:
            return None
        for m in re.finditer(r"^##\s+(.+)$", body, re.M):
            if want in m.group(1).strip().lower():
                nxt = re.search(r"^##\s", body[m.end():], re.M)
                content = body[m.end(): m.end() + nxt.start()] if nxt else body[m.end():]
                filled = len(re.sub(r"\s", "", content))
                return None if filled >= min_chars else \
                    f"section `## {m.group(1).strip()}` is present but empty/thin (<{min_chars} chars)"
        return f"body section matching `## …{rule.get('section')}…` is missing"
    if kind == "freshness":
        f = rule.get("field") or "updated"
        max_age = int(rule.get("max_age_days") or 90)
        v = str(fm.get(f) or "")[:10]
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", v)
        if not m:
            return f"date field `{f}` is missing/unparseable"
        age = (date.today() - date(int(m.group(1)), int(m.group(2)), int(m.group(3)))).days
        return None if age <= max_age else f"`{f}` is {age}d old (max {max_age}d)"
    return None


def _gap_key(rule_id: str, rel: str) -> str:
    return f"{rule_id}--{rel.replace('/', '-')}"


def main() -> int:
    rules = load_rules()
    if rules is None:
        print(f"# no completeness rules at {RULES_FILE} — nothing to audit; see the "
              "extension README for the rule grammar (a pack DECLARES its expectations; "
              "this lane never guesses them)")
        print(json.dumps({"wakeAgent": False}))
        return 0

    pages, keys, by_type = _page_index()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # existing gap pages: key -> (path, fm)
    existing: dict[str, tuple[Path, dict]] = {}
    if GAPS.is_dir():
        for p in GAPS.rglob("*.md"):
            if p.name.startswith(("_", ".")) or p.name.startswith("INDEX"):
                continue
            fm, _ = _split(p)
            existing[p.stem] = (p, fm)

    opened = refreshed = resolved = 0
    saturated: list[str] = []
    live_keys: set[str] = set()
    per_rule: dict[str, dict] = {r["id"]: {"open": 0, "sat": False, "rule": r} for r in rules}

    for rule in rules:
        rid = rule["id"]
        hits = 0
        for rel, slug, fm, body in pages:
            if rel.startswith("gaps/") or not _selected(fm, rule["when"]):
                continue
            why = _unmet(rule, rel, slug, fm, body, keys, by_type)
            if why is None:
                continue
            key = _gap_key(rid, rel)
            live_keys.add(key)
            prior = existing.get(key)
            if prior and str(prior[1].get("status")) == "dismissed":
                continue                       # operator said no — never reopened
            hits += 1
            if hits > MAX_PER_RULE:
                if not per_rule[rid]["sat"]:
                    per_rule[rid]["sat"] = True
                    saturated.append(rid)
                continue
            per_rule[rid]["open"] += 1
            if prior:
                p, pfm = prior
                if str(pfm.get("status")) != "open" or str(pfm.get("expectation")) != why:
                    pass                        # rewrite below (reopened-from-resolved or changed)
                elif str(pfm.get("last_seen")) == today:
                    continue                    # already current
                refreshed += 1
                first_seen = str(pfm.get("first_seen") or today)
            else:
                opened += 1
                first_seen = today
            gp = GAPS / f"{_gap_key(rid, rel)}.md"
            gp.parent.mkdir(parents=True, exist_ok=True)
            fm_out = {"type": "gap", "rule": rid, "subject": rel,
                      "severity": rule.get("severity", "medium"), "status": "open",
                      "expectation": why, "first_seen": first_seen, "last_seen": today}
            if rule.get("resolution_hint"):
                fm_out["resolution_hint"] = rule["resolution_hint"]
            gp.write_text(
                "---\n" + yaml.safe_dump(fm_out, sort_keys=False, allow_unicode=True).strip()
                + "\n---\n"
                + f"# {rule.get('title') or rid}\n\n"
                + f"[[{rel}]] — {why}.\n\n"
                + (f"**Resolve:** {rule['resolution_hint']}\n" if rule.get("resolution_hint") else "")
                + f"\n_Deterministic completeness gap (rule `{rid}`, okengine.completeness). "
                  "Set `status: dismissed` + `dismiss_reason:` to suppress permanently — "
                  "dismissals feed the rule-precision table on [[dashboards/completeness]]._\n",
                encoding="utf-8")

    # auto-resolve: open gaps whose expectation is no longer unmet (or subject vanished)
    for key, (p, fm) in existing.items():
        if key in live_keys or str(fm.get("status")) != "open":
            continue
        fm["status"], fm["resolved_on"] = "resolved", today
        _, body = _split(p)
        p.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
                     + "\n---" + body, encoding="utf-8")
        resolved += 1

    # dashboard: queue by severity + per-rule precision
    counts = {"open": 0, "resolved": 0, "dismissed": 0}
    rows: list[tuple] = []
    rule_stats: dict[str, dict] = {r["id"]: {"open": 0, "resolved": 0, "dismissed": 0} for r in rules}
    if GAPS.is_dir():
        for p in sorted(GAPS.rglob("*.md")):
            if p.name.startswith(("_", ".")) or p.name.startswith("INDEX"):
                continue
            fm, _ = _split(p)
            st = str(fm.get("status") or "open")
            counts[st] = counts.get(st, 0) + 1
            rs = rule_stats.setdefault(str(fm.get("rule")), {"open": 0, "resolved": 0, "dismissed": 0})
            rs[st] = rs.get(st, 0) + 1
            if st == "open":
                rows.append((SEVERITIES.index(fm.get("severity")) if fm.get("severity") in SEVERITIES else 1,
                             str(fm.get("severity")), str(fm.get("rule")), str(fm.get("subject")),
                             str(fm.get("expectation") or ""), p.stem, str(fm.get("first_seen") or "")))
    rows.sort()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    L = ["---", "type: dashboard", 'title: "Completeness gaps"', f"updated: {now}", "---", "",
         f"# Completeness gaps — {now}", "",
         "_Deterministic gaps against the pack's declared expectations "
         f"(`{RULES_FILE}`, okengine.completeness). A gap is EXPLAINED by construction: "
         "rule + subject + the exact unmet expectation._", "",
         f"- open: **{counts.get('open', 0)}** · resolved: {counts.get('resolved', 0)} · "
         f"dismissed: {counts.get('dismissed', 0)}"
         + (f" · **saturated rules: {', '.join(saturated)}** (>{MAX_PER_RULE} open — capped, not hidden)"
            if saturated else ""), ""]
    if rows:
        L += ["## Open gaps", "", "| Sev | Rule | Subject | Unmet expectation | Since |", "|---|---|---|---|---|"]
        for _, sev, rid, subj, why, stem, since in rows[:500]:
            L.append(f"| {sev} | `{rid}` | [[{subj}]] | {why} ([[gaps/{stem}|gap]]) | {since} |")
        L.append("")
    L += ["## Rule precision", "",
          "_A rule whose gaps are mostly dismissed is a rule to fix or retire._", "",
          "| Rule | Open | Resolved | Dismissed | Dismissal rate |", "|---|---|---|---|---|"]
    for rid, st in sorted(rule_stats.items()):
        tot = st["open"] + st["resolved"] + st["dismissed"]
        dr = f"{st['dismissed'] / tot:.0%}" if tot else "—"
        L.append(f"| `{rid}` | {st['open']} | {st['resolved']} | {st['dismissed']} | {dr} |")
    L.append("")
    out = WIKI / "dashboards" / "completeness.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")

    print(f"completeness-audit: {len(rules)} rule(s) over {len(pages)} pages — "
          f"{opened} opened, {refreshed} refreshed, {resolved} auto-resolved, "
          f"{counts.get('dismissed', 0)} dismissed (respected)"
          + (f"; SATURATED: {', '.join(saturated)}" if saturated else ""))
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
