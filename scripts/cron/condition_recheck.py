#!/usr/bin/env python3
"""condition_recheck — clear a review flag whose defect has since been repaired (okengine#547).

`needs_review` is a one-way latch: the enforced write path lets agents RAISE it and never clear it,
and the only exit — `review_autoverify` (#313) — clears by SOURCE-GRADING arithmetic. So a page
flagged for a MECHANICAL defect stays flagged forever, because the sole exit asks "is the evidence
good?", a question with nothing to do with why the page was flagged.

Measured on okcti-test: of 419 queue rows citing unresolvable wikilinks, **290 point at pages whose
wikilinks now all resolve**. `broken-wikilinks-drain` has been repairing them every two hours and
nothing ever cleared the flag it fixed. The work was done; the queue never noticed.

This lane closes that loop. For each flagged page it reads the OPEN review records that explain WHY
it was flagged (`wiki/operational/reviews/*.yaml`, whose `reasons[].detail` carries the original
write-path flag text) and re-tests only the conditions that are re-testable state:

  unresolvable wikilink   do all body wikilinks resolve now?
  degenerate              does the repetition-loop signature still match?
  slug id collision       the page exists, so the rejected create is moot

A page clears ONLY when every open reason is one of those classes AND every one now re-tests clean.
Anything else — a grounding failure, a conflict, a field-loss notice, `changed-after-approval`, or a
`legacy-unspecified` flag whose cause was never recorded — holds the page, because the flag may
exist BECAUSE of it. A flagged page with no open review record also holds: without a recorded reason
there is nothing to re-test, and "I cannot tell why this was flagged" is not evidence that it is
fixed.

The same refusals `review_autoverify` applies are applied here (judgment type, tombstoned,
conflicts, grounding failure, missing required field), and a cleared page is stamped with an
auditable basis exactly as that lane stamps `auto_verified_basis`.

Drift: the re-test predicates MIRROR the write path's raisers (`write_server._unresolvable_link_flags`,
`_degeneration_flags`). They are re-implemented here because `write_server` is BAKED into the gateway
image while this lane is STAGED — importing across that boundary would break on the first image/stage
skew. `tests/cron/test_condition_recheck.py::test_predicates_agree_with_the_write_path` imports both
and asserts they agree on a battery of inputs, so a divergence fails the build instead of silently
clearing pages the write path still considers broken.

Env: WIKI_PATH (vault root, default /opt/vault). `--dry-run` reports without writing.
Pure script (no_agent): always emits {"wakeAgent": false}.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"

_FM_RE = re.compile(r"\A---[ \t]*\n(.*?\n)---(.*)\Z", re.S)
_NEEDS_RE = re.compile(r"^needs_review:[ \t]*[Tt]rue[ \t]*$", re.M)
_GROUNDING_FAIL = re.compile(
    r"##[ \t]+Grounding check.*?(unsupported|not[- ]found|not in source|contradict)", re.S | re.I)
_OPEN_STATES = {"open", "in-review", "changes-requested", "rejected"}
_JUDGMENT_TYPES = {"assessment", "proposition", "prediction", "hypothesis", "forecast"}

# --- mirrors of the write path's raisers (see the drift note in the module docstring) -------
_WIKILINK = re.compile(r"\[\[([^\]|#]+)")
_LINK_REVIEW_NS = ("concepts", "entities")
_DEGEN_FENCE = re.compile(r"```.*?```", re.DOTALL)
_DEGEN_WIKILINK = re.compile(r"\[\[[^\]]*\]\]")
_DEGEN_STOP = re.compile(r"[.!?;:\n,]")
_DEGEN_MAX_RUN = 250


def wikilink_resolves(wiki: Path, t: str) -> bool:
    """Mirror of write_server._wikilink_resolves: literal path, then first- and second-letter shard."""
    if (wiki / f"{t}.md").is_file():
        return True
    parts = t.split("/")
    if len(parts) >= 2 and parts[-1][:1].isalnum():
        ns, base = parts[0], parts[-1]
        b = base[0].lower()
        if (wiki / ns / b / f"{base}.md").is_file():
            return True
        if len(base) > 1 and (wiki / ns / b / base[1].lower() / f"{base}.md").is_file():
            return True
    return False


def unresolvable_links(wiki: Path, namespace: str, body: str) -> list[str]:
    """Mirror of write_server._unresolvable_link_flags — the bad-link list, not its message."""
    if namespace not in _LINK_REVIEW_NS or not body:
        return []
    bad, seen = [], set()
    for m in _WIKILINK.finditer(body):
        t = (m.group(1) or "").strip().strip("/")
        if t.endswith(".md"):
            t = t[:-3]
        if not t or t in seen:
            continue
        seen.add(t)
        if "/" not in t:
            bad.append(f"[[{t}]] (bare name)")
        elif not wikilink_resolves(wiki, t):
            bad.append(f"[[{t}]] (no such page)")
    return bad


def is_degenerate(body: str) -> bool:
    """Mirror of write_server._degeneration_flags."""
    if not body:
        return False
    prose = _DEGEN_WIKILINK.sub(" ", _DEGEN_FENCE.sub("\n", body))
    return max((len(seg.split()) for seg in _DEGEN_STOP.split(prose)), default=0) > _DEGEN_MAX_RUN


# --- reason classification --------------------------------------------------
# Keyed off the write path's own flag text, carried verbatim in reasons[].detail.
_RETESTABLE = {
    "wikilink": re.compile(r"unresolvable wikilink", re.I),
    "degenerate": re.compile(r"\bdegenerate:", re.I),
    "collision": re.compile(r"slug id collision", re.I),
}


def classify(detail: str) -> str | None:
    """The re-testable class for a recorded reason, or None if it is not re-testable state."""
    for name, pat in _RETESTABLE.items():
        if pat.search(detail or ""):
            return name
    return None


def _namespace(rel: str) -> str:
    return rel.split("/")[0] if "/" in rel else ""


QUEUE_ROW = re.compile(r"^- \d{4}-\d{2}-\d{2} \*\*(?P<path>[^*]+)\*\* — (?P<reason>.*)$", re.M)


def queue_reasons(wiki: Path) -> dict[str, list[str]]:
    """subject (no .md) -> [reason, ...] from wiki/_review-queue.md.

    The review-record store is NOT sufficient on its own: on okcti-test it explains only ~200 of
    2,454 flagged pages, because records predate a reshard (their `subject` is a stale path) or the
    flag came from `_flag`, which writes a queue row and no record at all. The queue log carries the
    write path's verbatim flag text for both producers, and review-reconcile (#540) now keeps its
    paths current — so it is the better reason source, and the records supplement it.
    """
    q = wiki / "_review-queue.md"
    out: dict[str, list[str]] = defaultdict(list)
    try:
        text = q.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for m in QUEUE_ROW.finditer(text):
        rel = m.group("path").strip().removesuffix(".md")
        out[rel].append(m.group("reason").strip())
    return out


def open_reasons(wiki: Path) -> dict[str, list[str]]:
    """subject (no .md) -> [reason detail, ...] across every OPEN review record."""
    store = wiki / "operational" / "reviews"
    out: dict[str, list[str]] = defaultdict(list)
    if not store.is_dir():
        return out
    # glob-ok: write_server._review_record_path writes every record flat as <digest>.yaml here.
    for rp in sorted(store.glob("*.yaml")):  # glob-ok: flat record store
        # Two single-class handlers rather than a tuple: an unreadable file and an unparseable
        # one are different failures, and the tuple form made cosmic-ray's ExceptionReplacer
        # escape the worker (an infrastructure error the gate cannot disposition).
        try:
            raw = rp.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            rec = yaml.safe_load(raw)
        except yaml.YAMLError:
            continue
        if not isinstance(rec, dict):
            continue
        if str(rec.get("state") or "").strip().lower() not in _OPEN_STATES:
            continue
        subject = str(rec.get("subject") or "").strip().removesuffix(".md")
        if not subject:
            continue
        for r in (rec.get("reasons") or []):
            if isinstance(r, dict) and str(r.get("detail") or "").strip():
                out[subject].append(str(r["detail"]))
    return out


def _frontmatter(text: str) -> dict:
    m = _FM_RE.match(text)
    if not m:
        return {}
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return fm if isinstance(fm, dict) else {}


def _refusal(schema: dict, fm: dict, text: str) -> str | None:
    """The same refusals review_autoverify applies — anything else wrong keeps the page held."""
    ptype = str(fm.get("type") or "")
    if ptype in _JUDGMENT_TYPES:
        return f"judgment type '{ptype}'"
    if str(fm.get("status") or "").lower() == "tombstoned":
        return "tombstoned"
    if isinstance(fm.get("conflicts"), list) and fm["conflicts"]:
        return "conflicts present"
    if _GROUNDING_FAIL.search(text):
        return "grounding-check failure"
    spec = (schema.get("types") or {}).get(ptype)
    req = (spec or {}).get("required") if isinstance(spec, dict) else None
    missing = [str(f) for f in (req or []) if str(f) != "type" and fm.get(str(f)) in (None, "", [], {})]
    if missing:
        return f"missing required: {', '.join(missing)}"
    return None


def evaluate(wiki: Path, rel: str, text: str, fm: dict, reasons: list[str],
             schema: dict) -> tuple[bool, str]:
    """(clearable, basis-or-reason-held) for one flagged page."""
    if not reasons:
        return False, "no open review record — the flag's cause was never recorded"
    refusal = _refusal(schema, fm, text)
    if refusal:
        return False, refusal
    classes = []
    for detail in reasons:
        cls = classify(detail)
        if cls is None:
            return False, f"not re-testable: {detail[:70]}"
        classes.append(cls)

    m = _FM_RE.match(text)
    body = m.group(2) if m else text
    still = []
    for cls in sorted(set(classes)):
        if cls == "wikilink":
            bad = unresolvable_links(wiki, _namespace(rel), body)
            if bad:
                still.append(f"{len(bad)} unresolvable wikilink(s) remain")
        elif cls == "degenerate":
            if is_degenerate(body):
                still.append("repetition-loop signature still present")
        # `collision` is moot by construction: the page exists, so the rejected create is history.
    if still:
        return False, "; ".join(still)
    return True, "re-tested clean: " + ", ".join(sorted(set(classes)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = ap.parse_args(argv)

    if not WIKI.is_dir():
        print(f"ERROR: wiki not found at {WIKI}", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 1

    schema = schema_lib.merged_schema(VAULT)
    # Union of both reason sources — a page clears only if EVERY recorded reason re-tests clean,
    # so merging can only make the lane more conservative, never less.
    reasons_by_subject = queue_reasons(WIKI)
    for subject, details in open_reasons(WIKI).items():
        reasons_by_subject[subject].extend(details)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cleared = 0
    held: Counter = Counter()

    for p in sorted(WIKI.rglob("*.md")):
        if p.name.startswith(("_", ".")) or p.name.upper().startswith("INDEX") or ".bak" in p.name:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _FM_RE.match(text)
        if not m or not _NEEDS_RE.search(m.group(1)):
            continue
        fm = _frontmatter(text)
        if fm.get("needs_review") is not True:
            continue
        rel = p.relative_to(WIKI).as_posix()
        ok, why = evaluate(WIKI, rel, text, fm, reasons_by_subject.get(rel[:-3], []), schema)
        if not ok:
            held[why.split(":")[0][:48]] += 1
            continue
        stamp = (f"review_status: condition-recheck-cleared\n"
                 f"recheck_basis: {json.dumps(why)}\n"
                 f"recheck_at: '{now}'")
        cleared += 1
        print(f"  clear {rel}: {why}")
        if not args.dry_run:
            p.write_text(f"---\n{_NEEDS_RE.sub(stamp, m.group(1), count=1).rstrip()}\n---\n\n"
                         f"{m.group(2).lstrip()}", encoding="utf-8")

    print(f"condition-recheck: {cleared} cleared, {sum(held.values())} held"
          f"{' [dry-run]' if args.dry_run else ''}")
    for why, n in held.most_common(8):
        print(f"  held {n}: {why}")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
