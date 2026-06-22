#!/usr/bin/env python3
"""Validate the {{PACK}} domain pack — parse + cross-consistency checks.

Runs with no engine checkout and no Docker: it only reads this repo. Catches the
class of bug that ships silently in a config/data pack — a YAML/JSON/OPML parse
error, or drift between what the crons write and what schema.yaml declares (e.g. a
cron writing to a namespace that has no partitioning/tier rule).

Also enforces the USEFUL-BY-DEFAULT invariant: domain crons ship ENABLED (so the
pack runs out of the box) but the active feeds.opml ships EMPTY — so a fresh
install makes zero upstream calls until the operator populates feeds.opml. Enabled
crons must use a `@jitter:*` schedule (expanded to a random minute per install) so
that, once feeds go live, installs don't hit upstream on the same minute. The
curated source list lives in feeds.opml.example (copy it into feeds.opml to start).

Usage:
    python3 validate.py            # parse + consistency checks (offline, fast)
    python3 validate.py --fix      # rewrite the feeds.opml.example count comment if it drifted
    python3 validate.py --probe    # HTTP-probe the suggested feeds in feeds.opml.example (network)

Exit 0 = all checks pass. Exit 1 = at least one FAIL (warnings never fail the run).
Dependency: PyYAML (schema.yaml). Everything else is stdlib.
"""
import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent

# wiki/<file>.md sitting at the vault root are engine-managed files, not namespaces.
ROOT_WIKI_FILES = {"index", "HOT", "log", "_review-queue"}

# Namespaces THIS pack requires to stay human-only (regression guard). Most domains
# have none — the generator ships an empty list; a pack opts in by listing namespaces.
REQUIRED_HUMAN_ONLY = []  # e.g. ["findings"] to require a human-only gate

# Safe-by-default: the pack ships NO active sources. feeds.opml is empty; the curated
# suggestion list lives in feeds.opml.example (operators copy from it to opt in).
ACTIVE_OPML = "feeds/feeds.opml"
SUGGEST_OPML = "feeds/feeds.opml.example"

fails: list[str] = []
warns: list[str] = []


def fail(msg: str) -> None:
    fails.append(msg)


def warn(msg: str) -> None:
    warns.append(msg)


def load_json(rel: str):
    return json.loads((ROOT / rel).read_text())


def load_yaml(rel: str):
    return yaml.safe_load((ROOT / rel).read_text())


def check_parse() -> dict:
    """Every machine-readable file parses. Returns the parsed schema for later checks."""
    for rel in ("crons/domain-crons.json", "crons/engine-template-prompts.json"):
        try:
            load_json(rel)
        except Exception as e:  # noqa: BLE001
            fail(f"{rel}: invalid JSON — {e}")
    schema = {}
    try:
        schema = load_yaml("schema.yaml")
    except Exception as e:  # noqa: BLE001
        fail(f"schema.yaml: invalid YAML — {e}")
    return schema or {}


def _parse_opml(rel: str) -> tuple[list[str] | None, str]:
    """Parse an OPML file. Returns (urls, raw_text); urls is None if the file is absent
    or invalid XML (the latter is reported as a failure)."""
    path = ROOT / rel
    if not path.exists():
        return None, ""
    try:
        tree = ET.parse(path)
    except Exception as e:  # noqa: BLE001
        fail(f"{rel}: invalid XML — {e}")
        return None, ""
    urls = [o.get("xmlUrl") for o in tree.iter("outline") if o.get("xmlUrl")]
    for u in urls:
        if not u.startswith(("http://", "https://")):
            fail(f"{rel}: feed URL is not http(s): {u}")
    return urls, path.read_text()


def check_feeds() -> list[str]:
    """Validate feeds. Safe-default: the ACTIVE list is empty (nothing fetched on a
    fresh clone); the curated SUGGESTION list must exist and keep an honest count.
    Returns the suggested URLs (what --probe checks)."""
    active, _ = _parse_opml(ACTIVE_OPML)
    if active is None:
        warn(f"{ACTIVE_OPML} missing (no active sources — safe default)")
    elif not active:
        warn(f"{ACTIVE_OPML} is empty — crons run but ingest nothing until you "
             f"populate it (copy entries from feeds.opml.example). This is the "
             f"expected out-of-the-box state; the single step to go live.")
    # else: operator has deliberately enabled sources — fine.

    suggested, text = _parse_opml(SUGGEST_OPML)
    if suggested is None:
        fail(f"{SUGGEST_OPML} missing — the curated suggested-source list")
        return []
    if not suggested:
        warn(f"{SUGGEST_OPML} lists no feeds")
        return []
    # The header comment claims a count — a stale count fails the run (CI blocks).
    # --fix runs before this check and repairs it; locally, `python3 validate.py --fix`.
    m = re.search(r"All (\d+) probed", text)
    if m and int(m.group(1)) != len(suggested):
        fail(f"{SUGGEST_OPML} comment says {m.group(1)} feeds but {len(suggested)} are "
             f"listed (run `python3 validate.py --fix`)")
    return suggested


def fix_feed_count() -> bool:
    """Rewrite the suggested OPML's 'All N probed' comment to the live feed count.

    Returns True if the file was changed. A no-op (and no write) when already correct.
    """
    path = ROOT / SUGGEST_OPML
    if not path.exists():
        return False
    try:
        tree = ET.parse(path)
    except Exception:  # noqa: BLE001  (parse errors are reported by check_feeds)
        return False
    n = sum(1 for o in tree.iter("outline") if o.get("xmlUrl"))
    text = path.read_text()
    new = re.sub(r"All \d+ probed", f"All {n} probed", text)
    if new != text:
        path.write_text(new)
        print(f"FIXED  {SUGGEST_OPML} comment -> All {n} probed")
        return True
    return False


def check_crons_jittered() -> None:
    """Useful-by-default invariant: domain crons MAY (and by default DO) ship
    enabled — feed-fetch is harmless while feeds.opml is empty (it makes zero
    upstream calls and logs a clean no-op). But an enabled cron must NOT commit a
    herd-prone fixed schedule: it must use a `@jitter:*` sentinel (expanded to a
    random minute per install at framework init/pull) or an already-jittered
    concrete expr (a non-:00 minute). A committed `0 */2 * * *` would point every
    install at upstream on the same minute."""
    try:
        crons = load_json("crons/domain-crons.json")
    except Exception:  # noqa: BLE001  (JSON parse already reported by check_parse)
        return
    for item in crons:
        if item.get("enabled") is not True:
            continue
        expr = ((item.get("schedule") or {}).get("expr") or "").strip()
        name = item.get("name")
        if expr.startswith("@jitter:"):
            continue  # expanded to a random minute at install
        minute = expr.split()[0] if expr else ""
        if minute in ("", "0", "*", "*/1"):
            fail(f"domain cron '{name}' is enabled with a herd-prone schedule "
                 f"'{expr}' — use a @jitter:* sentinel (preferred) or a non-:00 "
                 f"minute so installs don't hit upstream on the same minute")


def cron_prompt_strings() -> list[str]:
    """All free-text prompt strings the agents read, across both cron files."""
    out: list[str] = []
    for item in load_json("crons/domain-crons.json"):
        if isinstance(item.get("prompt"), str):
            out.append(item["prompt"])
    out.extend(v for v in load_json("crons/engine-template-prompts.json").values() if isinstance(v, str))
    return out


def known_namespaces(schema: dict) -> set[str]:
    part = (schema.get("partitioning") or {}).get("namespaces") or {}
    excluded = {Path(p.rstrip("/")).name for p in (schema.get("exclude") or [])}
    return set(part) | excluded


def check_namespace_consistency(schema: dict) -> None:
    """Every wiki/<dir>/ a cron writes to must be a schema namespace or excluded."""
    known = known_namespaces(schema)
    for prompt in cron_prompt_strings():
        for seg in re.findall(r"wiki/([a-z_]+)/", prompt):
            if seg not in known:
                fail(f"cron writes to wiki/{seg}/ but it is not a schema namespace "
                     f"or exclude (known: {sorted(known)})")
        for seg in re.findall(r"wiki/([A-Za-z_]+)\.md", prompt):
            if seg not in ROOT_WIKI_FILES:
                warn(f"cron references wiki/{seg}.md (not a known engine root file)")


def check_type_consistency(schema: dict) -> None:
    """Every literal `type: X` a cron tells the agent to write must be a schema type."""
    types = set(schema.get("types") or {})
    for prompt in cron_prompt_strings():
        for t in re.findall(r"type:\s+([a-z][a-z-]+)", prompt):
            if t not in types:
                fail(f"cron writes frontmatter `type: {t}` but it is not a schema type "
                     f"(declared: {sorted(types)})")


def check_human_only_gates(schema: dict) -> None:
    """Validate human-only write gates GENERICALLY (works for any domain):
      - any namespace declared create:false & update:false must have EXACTLY that
        shape (no stray keys);
      - every namespace in REQUIRED_HUMAN_ONLY must be present and gated.
    Absence of any gate is legal — most domains have none."""
    ns = (schema.get("permissions") or {}).get("namespaces") or {}
    for name, perms in ns.items():
        if not isinstance(perms, dict):
            continue
        if perms.get("create") is False and perms.get("update") is False:
            if perms != {"create": False, "update": False}:
                warn(f"permissions.{name} looks human-only but has extra keys: {perms}")
    for name in REQUIRED_HUMAN_ONLY:
        if ns.get(name) != {"create": False, "update": False}:
            fail(f"permissions.{name} must stay human-only {{create:false, update:false}}, "
                 f"got: {ns.get(name)}")


def check_schema_consistency(schema: dict) -> None:
    """Cross-section drift: tier / hot_set must reference real partitioning namespaces."""
    part = set((schema.get("partitioning") or {}).get("namespaces") or {})
    excluded = {Path(p.rstrip("/")).name for p in (schema.get("exclude") or [])}
    tier_ns = set((schema.get("tier") or {}).get("namespaces") or {})
    perms = (schema.get("permissions") or {}).get("namespaces") or {}
    human_only = {n for n, p in perms.items()
                  if isinstance(p, dict) and p.get("create") is False and p.get("update") is False}
    for ns in sorted(tier_ns - part):
        fail(f"tier.namespaces['{ns}'] is not declared in partitioning.namespaces")
    for ns in sorted(part - tier_ns - human_only):  # human-only ns may be deliberately untiered
        warn(f"partitioning namespace '{ns}' has no tier entry (won't be tiered hot/warm/cold)")
    for sec in (schema.get("hot_set") or {}).get("sections") or []:
        ns = sec.get("namespace")
        if ns and ns not in part and ns not in excluded:
            fail(f"hot_set section namespace '{ns}' is not a partitioning namespace or exclude")


def check_namespace_dirs_exist(schema: dict) -> None:
    """Each on-disk wiki namespace should be a real directory (keeps the tree honest)."""
    for ns in (schema.get("partitioning") or {}).get("namespaces") or {}:
        if not (ROOT / "wiki" / ns).is_dir():
            warn(f"schema namespace '{ns}' has no wiki/{ns}/ directory (add a .gitkeep)")


def check_pack_meta(schema: dict) -> None:
    """pack.yaml (composable-okpacks identity) is well-shaped and self-consistent:
    it must parse, name itself, declare a trust level, and only claim ownership of
    types/namespaces this pack actually declares in schema.yaml. The engine
    re-validates the WHOLE composed set (disjoint ownership across packs) at deploy;
    this catches the single-pack errors offline."""
    path = ROOT / "pack.yaml"
    if not path.exists():
        warn("pack.yaml missing — needed for composable-okpacks deployment")
        return
    try:
        meta = yaml.safe_load(path.read_text())
    except Exception as e:  # noqa: BLE001
        fail(f"pack.yaml: invalid YAML — {e}")
        return
    if not isinstance(meta, dict) or not meta.get("name"):
        fail("pack.yaml must be a mapping with a `name`")
        return
    if meta.get("trust") not in ("public", "private"):
        fail(f"pack.yaml trust must be 'public' or 'private' (got {meta.get('trust')!r})")
    owns = meta.get("owns") if isinstance(meta.get("owns"), dict) else {}
    types = set(schema.get("types") or {})
    for t in owns.get("types") or []:
        if t not in types:
            fail(f"pack.yaml owns type '{t}' but it is not declared in schema.yaml types")
    known = known_namespaces(schema)
    for ns in owns.get("namespaces") or []:
        if ns not in known:
            warn(f"pack.yaml owns namespace '{ns}' with no partitioning/exclude entry in schema.yaml")
    for req in meta.get("requires") or []:
        if not isinstance(req, str) or not req.strip():
            fail(f"pack.yaml requires entry is not a non-empty string: {req!r}")


def probe_feeds(urls: list[str]) -> None:
    import urllib.request

    for u in urls:
        try:
            req = urllib.request.Request(u, method="GET", headers={"User-Agent": "{{PACK}}-validate"})
            with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310
                if r.status != 200:
                    warn(f"feed probe: HTTP {r.status} — {u}")
        except Exception as e:  # noqa: BLE001
            warn(f"feed probe: unreachable — {u} ({e})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe", action="store_true", help="HTTP-probe every feed (network)")
    ap.add_argument("--fix", action="store_true", help="rewrite the feeds.opml count comment to match")
    args = ap.parse_args()

    if args.fix:
        fix_feed_count()

    schema = check_parse()
    suggested = check_feeds()
    if schema:
        check_namespace_consistency(schema)
        check_type_consistency(schema)
        check_human_only_gates(schema)
        check_schema_consistency(schema)
        check_namespace_dirs_exist(schema)
        check_pack_meta(schema)
    check_crons_jittered()
    if args.probe:
        probe_feeds(suggested)

    for w in warns:
        print(f"WARN  {w}")
    for f in fails:
        print(f"FAIL  {f}")
    if fails:
        print(f"\n{len(fails)} failure(s), {len(warns)} warning(s).")
        return 1
    print(f"OK — pack valid ({len(suggested)} suggested feeds), {len(warns)} warning(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
