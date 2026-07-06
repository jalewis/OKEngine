#!/usr/bin/env python3
"""OKEngine MCP WRITE surface (ENGINE, conformance gap G1).

A SEPARATE, local stdio MCP server that exposes vault-WRITE tools. It is the
server-enforced conformance contract for agent writes: every write tool
validates the composed page against the governing `schema.yaml` (walk-up, via
`tools.schema_validator.schema_reject_reason`) BEFORE touching the filesystem,
and appends one line to `wiki/log.md` on success. This is the unbypassable
counterpart to the `file`-tool write-guard (which stays as the backstop).

Distinct from the read-only `server.py` (networked, vault mounted `:ro`). Default
transport is STDIO (the trusted local gateway caller — full write). Set
OKENGINE_WRITE_TRANSPORT=streamable-http to expose a NETWORKED, token-authenticated
write surface so an out-of-process sidecar extension can write (okengine#132): the
admin token keeps full write; an extension's minted token is limited to its declared
write scopes, and its pages are stamped with `extension_id` provenance.

Tools (each returns a short human-readable status string):
  create_entity(path, frontmatter_yaml, body)         — refuses if file exists
  update_entity(path, frontmatter_yaml=None, body=None) — bumps version
  converge_entity(path, frontmatter_yaml, body, pack) — upsert by id; merge under
                                                        page+field ownership (P2)
  tombstone_entity(path, reason, superseded_by=None)  — never deletes
  flag_for_review(path, note)                          — appends to review queue

The real logic lives in plain module-level helpers (`_create`/`_update`/
`_tombstone`/`_flag`); the `@mcp.tool()` wrappers merely delegate. This keeps
the helpers unit-testable without the `mcp` package installed.

Env: WIKI_PATH (/opt/vault), OKENGINE_MCP_WRITE_DATE (override today()).
"""
from __future__ import annotations

import contextvars
import datetime
import difflib
import hmac
import os
import re
import sys
from pathlib import Path
from typing import Optional, Union

import yaml

# Robust import of the engine validator regardless of CWD: repo root is the
# parent of this file's directory (okengine-mcp/'s parent).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools.schema_validator import schema_reject_reason, governing_policy, drift_policy  # noqa: E402
import scope as _scope  # noqa: E402  per-extension token resolution (okengine#132)

# Converge-on-write (composable okpacks P2) needs the id + schema + merge libs.
# Optional: if any are absent, converge_entity is disabled but the rest works.
_CONVERGE_OK = True
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "cron"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import id_lib       # noqa: E402
    import schema_lib   # noqa: E402
    import id_index     # noqa: E402
    import converge     # noqa: E402
except Exception:       # pragma: no cover
    _CONVERGE_OK = False

# `or` (not a get() default): a set-but-blank WIKI_PATH must fall back too, else
# Path("")/"wiki" resolves to a *relative* wiki/ under CWD (okengine#34).
VAULT = Path(os.environ.get("WIKI_PATH") or "/opt/vault")
WIKI = VAULT / "wiki"
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---(.*)\Z", re.S)
# A TRUE H1 (`# title`). `[ \t]+` after the single `#` means `## Summary` and deeper
# section headings never match — only a page-title H1 is captured (group 1).
_H1 = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.M)


def _today() -> str:
    """ISO date (YYYY-MM-DD) for the wiki/log.md ledger lines. Injectable for tests."""
    override = os.environ.get("OKENGINE_MCP_WRITE_DATE")
    if override:
        return override
    return datetime.date.today().isoformat()


def _now() -> str:
    """ISO-8601 UTC TIMESTAMP (YYYY-MM-DDTHH:MM:SSZ) for `last_updated`/`created`/`updated` — the
    OKF envelope fields the spec defines as timestamps (guide-2), so the UI can track *when*, not
    just *which day*. Injectable via OKENGINE_MCP_WRITE_NOW; falls back to the date override (so a
    date-only test override still works) then real UTC now."""
    override = os.environ.get("OKENGINE_MCP_WRITE_NOW")
    if override:
        return override
    date_override = os.environ.get("OKENGINE_MCP_WRITE_DATE")
    if date_override:
        return date_override
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wiki() -> Path:
    """Re-read WIKI from env each call so tests can repoint WIKI_PATH at runtime."""
    return Path(os.environ.get("WIKI_PATH") or str(VAULT)) / "wiki"


# Per-request caller identity (okengine#132), set by the networked auth middleware.
# None (stdio — the trusted local gateway caller) = admin = FULL write, which is the
# pre-#132 behavior. A networked extension caller is limited to its write scopes.
_caller_var: contextvars.ContextVar = contextvars.ContextVar("okengine_write_caller", default=None)


def _caller() -> dict:
    c = _caller_var.get()
    return c if c is not None else {"kind": "admin", "write_scopes": None, "ext_id": None}


def _authorize_write(path: str) -> bool:
    """May the current caller write this wiki-relative path? Admin (stdio gateway) =
    always; an extension = only within its declared write scopes."""
    c = _caller()
    if c.get("kind") == "admin":
        return True
    return _scope.path_in_scopes(str(path), c.get("write_scopes") or [])


def _wauth_refusal(path) -> Optional[str]:
    """Refuse if the caller can't write this path. Authorize on the NORMALIZED target, not the
    raw agent string: _safe() collapses '..' and strips redundant prefixes, so a raw
    'entities/../predictions/x' textually matches an 'entities/**' scope yet WRITES to
    predictions/ — scope must gate the real destination, not the spelling (okengine#178). Accepts
    a str or a resolved Path (converge re-auth passes the redirected canonical)."""
    sp = _safe(str(path))
    check = str(sp.relative_to(_wiki().resolve())) if sp is not None else str(path)
    if not _authorize_write(check):
        c = _caller()
        return (f"refused: '{path}' is outside extension '{c.get('ext_id')}'"
                f"'s write scope (declared: {c.get('write_scopes')})")
    return None


def _normalize_entity_shard(rel: str) -> str:
    """Canonicalize an entity path to the shard layout the reshard drain + assembler use, so an
    agent that picks the wrong shard doesn't create a stale DUPLICATE (okengine#48). The shard
    letters are always recomputed from the SLUG (not trusted from the path). The vault may RESHARD
    a hot first-letter leaf to two levels (`entities/<l>/<2nd>/<slug>.md`, 2nd = slug[1]) once it
    exceeds the threshold (reshard_oversized.py); this must NOT collapse a valid resharded canonical
    back to one level — that refuses/duplicates writes on a mature vault (okengine invariant-audit).
    Choose one- vs two-level by what's actually on disk. Other namespaces are left untouched."""
    parts = rel.split("/")
    # only the entities/ first-char-shard scheme (single-char intermediate segments); multi-char
    # segments are some other layout and are left alone.
    if not (len(parts) >= 3 and parts[0] == "entities"
            and all(len(seg) == 1 for seg in parts[1:-1])):
        return rel
    stem = parts[-1][:-3] if parts[-1].endswith(".md") else parts[-1]
    if not stem:
        return rel
    l1 = stem[0].lower()
    one = f"entities/{l1}/{stem}.md"
    second = stem[1].lower() if len(stem) > 1 and stem[1].isalnum() else "_"
    two = f"entities/{l1}/{second}/{stem}.md"
    try:
        wiki = _wiki()
        if (wiki / two).exists():
            return two                              # already at the resharded canonical
        leaf = wiki / "entities" / l1
        if (not (wiki / one).exists() and leaf.is_dir()
                and any(d.is_dir() and len(d.name) == 1 for d in leaf.iterdir())):
            return two                              # this first-letter leaf HAS been resharded
    except OSError:
        pass
    return one                                      # default / un-resharded: one level


def _safe(path: str) -> Optional[Path]:
    """Resolve a wiki-relative path, refusing escapes outside wiki/, forcing .md.

    Paths are relative to wiki/ (e.g. `sources/2026/06/x`). A caller (or pack
    ingest prompt) that prefixes a redundant leading `wiki/` must NOT stack into
    `wiki/wiki/...` — strip it. The escape guard can't catch that because the
    doubled path is still *inside* wiki/, so it would silently misfile every page
    and break raw-drain dedup (okengine#31). The same applies to an OVER-QUALIFIED
    path: an agent that follows the persona's "prefer the absolute form" guidance
    for file_read may pass the full `/opt/vault/wiki/sources/x` (or the vault-relative
    `opt/vault/wiki/...`) to a write tool — that would land in a shadow
    `wiki/opt/vault/wiki/...` tree (still inside wiki/, so the escape guard misses
    it), creating duplicate canonicals. Collapse any leading absolute/relative
    vault-or-wiki prefix to the wiki-relative tail first (the over-qualified-path
    variant of okengine#31/#34). Entity paths are also normalized to the one-level
    shard layout to prevent duplicate canonicals (okengine#48)."""
    wiki = _wiki()
    try:
        wiki_abs = wiki.resolve()
    except OSError:
        wiki_abs = wiki
    rel = path.strip()
    # Strip the longest matching over-qualified prefix: the absolute wiki path,
    # the absolute vault path, or either without the leading slash. Longest-first
    # so `/opt/vault/wiki` wins over `/opt/vault`; the redundant-`wiki/` loop below
    # then mops up any residual (e.g. a stripped vault prefix leaving `wiki/...`).
    _prefixes = []
    for _b in (wiki_abs, wiki_abs.parent):
        _s = str(_b)
        _prefixes += [_s, _s.lstrip("/")]
    for _cand in sorted({p for p in _prefixes if p}, key=len, reverse=True):
        if rel == _cand or rel.startswith(_cand + "/"):
            rel = rel[len(_cand):]
            break
    rel = rel.lstrip("/")
    while rel == "wiki" or rel.startswith("wiki/"):
        rel = rel[len("wiki"):].lstrip("/")
    rel = _normalize_entity_shard(rel)
    p = wiki / rel
    if p.suffix != ".md":
        p = p.with_suffix(".md")
    try:
        p = p.resolve()
        p.relative_to(wiki.resolve())
    except (OSError, ValueError):
        return None
    return p


_WIKILINK_FULL = re.compile(r"^\[\[\s*([^\]|#]+?)\s*(?:[#|][^\]]*)?\]\]$")


def _strip_wikilink(s):
    """'[[concepts/x]]' -> 'concepts/x' (drops any #anchor / |display); a non-wikilink value is
    returned unchanged."""
    if isinstance(s, str):
        m = _WIKILINK_FULL.match(s.strip())
        if m:
            return m.group(1).strip()
    return s


def _looks_like_ref_list(v) -> bool:
    """A list that needs canonicalizing: it either contains nested lists (the shape YAML produces
    when a bare `[[x]]` wikilink is used as a value — `[[x]]` parses as a nested flow sequence) or
    holds `[[..]]` wikilink strings."""
    return isinstance(v, list) and (
        any(isinstance(x, list) for x in v)
        or any(isinstance(x, str) and x.strip().startswith("[[") for x in v))


def _flatten_strip(v) -> list:
    """Flatten arbitrarily-nested lists (the `[[..]]` YAML mangling) into a flat list of plain
    wiki-relative path strings, wikilink-stripped, dropping blanks + dups (order-preserving)."""
    out: list = []
    def walk(x):
        if isinstance(x, list):
            for y in x:
                walk(y)
            return
        s = _strip_wikilink(x)
        if isinstance(s, str):
            s = s.strip()
        if s and s not in out:
            out.append(s)
    walk(v)
    return out


def _normalize_refs(fm: dict) -> dict:
    """Canonicalize frontmatter reference values to plain wiki-relative path strings.

    Agents are trained to write `[[wikilinks]]`, but `[[x]]` in YAML is flow syntax for a NESTED
    sequence, so a bare wikilink in a frontmatter value silently mangles (`field_mapped: [[c/x]]`
    -> `[[ "c/x" ]]`; a `see_also` list -> `- - - c/x`). Wikilinks are a *body* convention;
    frontmatter holds structured data. Here, at the single enforced-write chokepoint (so every
    extension's writes are fixed at once), we coerce: a bare `[[x]]` string -> `x`; a list that
    mangled into nested lists, or holds `[[..]]` strings, -> a flat list of plain paths. Plain
    strings and plain lists are left untouched."""
    if not isinstance(fm, dict):
        return fm
    for k, v in list(fm.items()):
        if isinstance(v, str):
            fm[k] = _strip_wikilink(v)
        elif _looks_like_ref_list(v):
            fm[k] = _flatten_strip(v)
    return fm


def _coerce_fm(frontmatter_yaml: Union[str, dict, None]) -> Optional[dict]:
    """Accept a YAML string OR a dict; return a dict (or None to signal a parse
    error vs an empty/absent value, which returns {}). Frontmatter reference values are
    canonicalized (wikilink -> plain path) via _normalize_refs."""
    if frontmatter_yaml is None:
        return {}
    if isinstance(frontmatter_yaml, dict):
        return _normalize_refs(dict(frontmatter_yaml))
    try:
        loaded = yaml.safe_load(frontmatter_yaml)
    except Exception:
        return None
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        return None
    return _normalize_refs(loaded)


def _compose(fm: dict, body: str) -> str:
    """Render frontmatter + body into a page, preserving key order."""
    fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip("\n")
    body = body or ""
    return f"---\n{fm_text}\n---\n{body}"


def _read_page(p: Path) -> tuple[dict, str]:
    """Split an existing page into (frontmatter dict, body). Empty fm on no match."""
    text = p.read_text(encoding="utf-8", errors="replace")
    m = _FM.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        fm = {}
    if not isinstance(fm, dict):
        fm = {}
    body = m.group(2)
    if body.startswith("\n"):
        body = body[1:]
    return fm, body


def _frontmatter_error(p: Path) -> Optional[str]:
    """Return a refusal reason when a page appears to have frontmatter but it is
    malformed. Pages with no frontmatter are left to schema validation."""
    text = p.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return None
    m = _FM.match(text)
    if not m:
        return "existing page has malformed YAML frontmatter delimiters"
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception as e:
        return f"existing page has invalid frontmatter YAML: {str(e)[:120]}"
    if not isinstance(fm, dict):
        return "existing page has non-mapping frontmatter"
    return None


def _append_log(line: str) -> None:
    wiki = _wiki()
    wiki.mkdir(parents=True, exist_ok=True)
    log = wiki / "log.md"
    with log.open("a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def _rel(p: Path) -> str:
    try:
        return p.relative_to(_wiki()).as_posix()
    except ValueError:
        return p.as_posix()


# OKF reserved + engine-managed structural files. These are NOT agent-writable
# knowledge pages — log.md is appended by the server itself (_append_log), the
# INDEX tree is rebuilt by a cron, HOT.md is derived, the review queue is
# server-managed, and `_`-prefixed files are internal. Writing them through the
# entity tools would (e.g.) inject a YAML frontmatter block into a plain
# changelog. Every agent-facing write helper refuses them up front.
_RESERVED_NAMES = {"log.md", "index.md", "agents.md", "hot.md", "readme.md"}


def _reserved_refuse(p: Path) -> Optional[str]:
    n = p.name.lower()
    if n in _RESERVED_NAMES or n.startswith("index-p") or p.name.startswith("_"):
        return (f"refused: {_rel(p)} is an engine-managed structural/reserved file "
                "— not agent-writable via the MCP write tools (use the file tool only "
                "if a human edit is truly intended)")
    return None


# --- G2/G3 write-governance: structural permissions + REVIEW FLAGS ---------
# The MCP write tools are the ENFORCED write path (the file-tool guard stays the
# schema/shape backstop). The pack declares policy in schema.yaml (read via
# tools.schema_validator.governing_policy, walk-up). Two distinct mechanisms:
#
#   1. STRUCTURAL permissions (HARD, rare) — `permissions.{default,namespaces}`:
#      per-namespace create/update rights (default: both allowed) and
#      delete:false everywhere (a knowledge page is tombstoned, never hard-rm'd —
#      data safety, not a review gate). A create/update-denied namespace is a real
#      structural boundary (e.g. a human-authored namespace), defaulting open.
#
#   2. REVIEW FLAGS (SOFT — flag, never gate) — `review.*`:  at 40k+ docs a hard
#      "needs human approval" GATE is impractical, so high-stakes agent
#      assertions are not blocked — the write SUCCEEDS and the page is FLAGGED
#      (`needs_review: true` + a wiki/_review-queue.md entry + a log note) so a
#      human / the UI can highlight it. Triggers: an agent asserting/escalating a
#      categorical `confidence` verdict (confirmed/false-positive/refuted —
#      numeric + low/med/high never flag), or setting/changing a configured
#      `review_on_change_field`. Preserving an already-present value never flags.

def _namespace(p: Path) -> str:
    """Knowledge namespace for a page (e.g. 'predictions', 'entities').

    Sub-domain aware (okengine#173 walk-up multipack): in a co-installed vault a page lives at
    ``wiki/<subdomain>/<namespace>/…`` where the sub-domain dir carries its OWN schema.yaml — the
    namespace is the dir BELOW that container, not the container itself. Reading the container as
    the namespace silently broke the enforced write path for every sub-domain vault: per-namespace
    create/update permissions never matched (the human-only `findings` guard was BYPASSED on
    update/patch/tombstone), and every create was rejected as an 'undeclared namespace'. For a
    flat vault (no nested schema.yaml) the loop never advances, so this is identical to the old
    ``parts[0]`` behavior."""
    try:
        rel = p.relative_to(_wiki())
    except ValueError:
        return ""
    parts = rel.parts
    if not parts:
        return ""
    wiki = _wiki()
    i = 0
    # skip leading sub-domain container dirs (each carries its own schema.yaml); never the filename
    while i < len(parts) - 1 and (wiki.joinpath(*parts[: i + 1]) / "schema.yaml").is_file():
        i += 1
    return parts[i]


def _ns_perm(policy: dict, ns: str) -> dict:
    perms = (policy or {}).get("permissions") or {}
    base = dict(perms.get("default") or {})
    nscfg = (perms.get("namespaces") or {}).get(ns) or {}
    base.update({k: v for k, v in nscfg.items() if k in ("create", "update", "delete")})
    return base


def _policy_reject(p: Path, fm: dict, op: str, prev: dict | None = None) -> Optional[str]:
    """HARD structural check only (namespace create/update rights). None => allowed.
    Review concerns are SOFT — see `_review_flags`, which never blocks a write."""
    policy = governing_policy(str(p))
    if not policy:
        return None
    ns = _namespace(p)
    perm = _ns_perm(policy, ns)
    if op == "create" and perm.get("create") is False:
        return f"namespace '{ns}' is not agent-writable (create denied; human-authored)"
    if op == "update" and perm.get("update") is False:
        return f"namespace '{ns}' is not agent-writable (update denied; human-authored)"
    return None


def _namespace_reject(p: Path) -> Optional[str]:
    """A knowledge page must land in a schema-DECLARED namespace. The write tools take a
    literal agent-supplied path and `_namespace()` is just its top dir, so an agent can drift a
    `type: source` page into a stray `source/` (singular) instead of the schema's `sources/`
    — a fork the dashboards/index/assembler never see (okengine#115, same class as the cwd
    split-brain #110). Reject an undeclared namespace, offering the closest declared one as a
    hint. No-op when the pack declares no namespaces (nothing to enforce against); excluded
    engine-internal dirs (operational/, dashboards/) are allowed."""
    ns = _namespace(p)
    if not ns:
        return None
    try:
        schema = _governing(p)
        declared = schema_lib.knowledge_namespaces(schema)
        allowed = declared | schema_lib.excluded_dirs(schema)
    except Exception:                       # pragma: no cover - schema load is best-effort
        return None
    if not declared or ns in allowed:
        return None
    hint = difflib.get_close_matches(ns, sorted(declared), n=1)
    suggest = f" — did you mean '{hint[0]}/'?" if hint else ""
    return (f"namespace '{ns}/' is not declared in schema.yaml (declared knowledge "
            f"namespaces: {sorted(declared)}){suggest} — a page written here forks into a "
            f"stray tree the dashboards/index never see (okengine#115)")


def _review_flags(p: Path, fm: dict, prev: dict | None = None) -> list[str]:
    """Return review reasons (flag, do NOT block). prev=None => create. A value
    that is unchanged from `prev` never re-flags (so backfills/no-ops don't churn
    the review queue)."""
    policy = governing_policy(str(p))
    if not policy:
        return []
    review = policy.get("review") or {}
    flags: list[str] = []

    cfield = review.get("confidence_field") or "confidence"
    review_vals = {str(v).lower() for v in (review.get("confidence_review_values") or [])}
    if review_vals and cfield in fm:
        val = str(fm.get(cfield) or "").strip().lower()
        prev_val = str((prev or {}).get(cfield) or "").strip().lower() if prev is not None else None
        if val in review_vals and not (prev is not None and prev_val == val):
            flags.append(f"agent asserted categorical `{cfield}: {fm.get(cfield)}`")

    for k in (review.get("review_on_change_fields") or []):
        if fm.get(k) not in (None, "") and fm.get(k) != (prev or {}).get(k):
            flags.append(f"agent set/changed review field `{k}`")
    return flags


# OKF envelope + assembler/bookkeeping keys allowed on any page, so unknown-field flagging
# (okengine#46) targets only genuine domain drift, never the universal scaffolding.
_OKF_ALWAYS = {"type", "name", "title", "tlp", "version", "last_updated", "created", "updated",
               "needs_review", "conflicts", "assembled_from", "aliases", "refs", "tags",
               "status", "id", "raw", "sources", "source"}


def _normalize_drift(fm: dict, p: Path) -> tuple[dict, list[str]]:
    """Converge frontmatter on the schema's vocabulary BEFORE write (okengine#46): rename alias
    keys to their canonical name, map aliased values, and surface unknown fields for review.
    Returns (normalized_fm, unknown-field flags). No-op when the pack declares no drift policy."""
    pol = drift_policy(str(p))
    if not pol:
        return fm, []
    out = dict(fm)
    for alias, canon in (pol.get("field_aliases") or {}).items():   # country -> suspected_origin
        if alias in out:
            v = out.pop(alias)
            if out.get(canon) in (None, "", [], {}):
                out[canon] = v
    for field, vmap in (pol.get("value_aliases") or {}).items():    # CN -> China ; active -> live
        if field in out and isinstance(vmap, dict):
            cur = out[field]
            out[field] = [vmap.get(x, x) for x in cur] if isinstance(cur, list) else vmap.get(cur, cur)
    flags: list[str] = []
    allowed = (pol.get("allowed") or {}).get(str(out.get("type") or ""))
    if isinstance(allowed, list):
        known = _OKF_ALWAYS | set(allowed) | set((pol.get("field_aliases") or {}).values())
        unknown = sorted(k for k in out if k not in known)
        if unknown:
            flags.append(f"unknown field(s) for type `{out.get('type')}` "
                         f"(not in schema): {', '.join(unknown)}")
    return out, flags


def _queue_review(p: Path, flags: list[str]) -> str:
    """Append a flagged page to wiki/_review-queue.md + log it. Returns a note to
    append to the tool result (empty if no flags). The write itself already
    succeeded — this only highlights, never blocks."""
    if not flags:
        return ""
    wiki = _wiki()
    wiki.mkdir(parents=True, exist_ok=True)
    queue = wiki / "_review-queue.md"
    if not queue.exists():
        queue.write_text(
            "---\ntitle: Review Queue\n---\n\n"
            "# Review Queue\n\nAgent-flagged pages awaiting human review "
            "(highlight, not a gate — the writes already landed).\n\n",
            encoding="utf-8",
        )
    reason = "; ".join(flags)
    with queue.open("a", encoding="utf-8") as f:
        f.write(f"- {_today()} **{_rel(p)}** — {reason}\n")
    _append_log(f"- {_today()} mcp-write review-flag {_rel(p)} — {reason}")
    return f" — flagged for review ({len(flags)} reason(s))"


# --- plain logic helpers (tested directly) -------------------------------

def _dedup_on_create(path: str, p: Path, fm: dict, body: str) -> Optional[str]:
    """Identity-based dedup for create_entity (okengine#98/#99/#100).

    The duplicate-canonical class is caused by `create_entity` keying on the
    on-disk PATH: every cosmetic variant of the same entity (different shard dir,
    wrong namespace, `Akira` vs `akira`, `vulnerability--cve-x` vs `cve-x`) is a
    new path, so it created a SECOND canonical the assembler never reconciles.
    The fix is to key on IDENTITY, not the filename: derive the page's stable id
    from its CONTENT (authority field, else minted slug) — exactly as converge
    does — and refuse to mint a second canonical for an id that already lives
    elsewhere. The path band-aids in !40/!41 fight this at the wrong layer; here
    the path is irrelevant to identity, which is the design intent (§5a of
    docs/design/composable-okpacks.md).

    Returns a result string when this handled the write (converged into the
    existing canonical, or flagged+refused a slug collision); None to let the
    normal create proceed. Mutates `fm` to stamp the derived `id` so the new page
    is resolvable forever after. No-op (returns None) when the converge/id libs
    are unavailable or no id can be derived."""
    if not _CONVERGE_OK:
        return None
    namespace = _namespace(p)
    try:
        schema = _governing(p)      # sub-domain aware (okengine#177); namespace is the bare mint scope
        pid, kind = _page_id_and_kind(fm, schema, namespace, p.stem)
    except Exception:               # pragma: no cover - id derivation is best-effort
        return None
    if not pid:
        return None
    fm["id"] = pid                  # stamp the content-derived id onto the new page
    existing_rel = _registry().resolve(pid)
    if not existing_rel:
        return None                 # genuinely new identity -> create normally
    existing_path = _wiki() / existing_rel
    if not existing_path.exists() or existing_path.resolve() == p.resolve():
        return None                 # stale index entry or the same page -> create normally
    # An existing canonical already owns this id at a different path.
    if kind == "authority":
        # Same real-world entity (authority ids are globally unique to the type)
        # -> converge into the canonical instead of duplicating it.
        return _converge(path, fm, body)
    # Minted-slug collision: two pages, possibly different entities, that slugged
    # the same. Never auto-merge a slug id -> flag for human review and refuse.
    _flag(path, f"slug id collision on create: {pid} already used by {existing_rel}")
    return (f"refused: slug id {pid} already used by {existing_rel} — "
            "flagged for review (slug ids never auto-merge)")


def _prov_pack() -> str:
    """The DEPLOYMENT's pack identity (pack.yaml `name`), injected as OKENGINE_PACK at deploy time.
    Deployment-pinned, never client/agent-supplied, so composition provenance can't be spoofed.
    Empty in a legacy single-pack deploy without the env (then provenance simply isn't stamped)."""
    return os.environ.get("OKENGINE_PACK", "").strip()


def _stamp_maintainer(fm: dict, *, creation: bool) -> None:
    """Composition provenance (okengine#90 P3): union this deployment's pack into `maintained_by`
    (the list of packs that have written the page) and, on CREATION, set `discovered_by` (the first
    attributor). Idempotent; a no-op when OKENGINE_PACK is unset."""
    pack = _prov_pack()
    if not pack:
        return
    prov = fm.get("maintained_by")
    prov = list(prov) if isinstance(prov, (list, tuple)) else ([prov] if prov else [])
    if pack not in prov:
        prov.append(pack)
    fm["maintained_by"] = prov
    if creation:
        fm.setdefault("discovered_by", pack)


# --- future-date guard ------------------------------------------------------
# The envelope's record-keeping dates say when a page WAS written/touched — a future value is
# always fabricated (a weekly-brief lane hallucinated published: <next Sunday> onto an empty
# stub, despite its prompt explicitly forbidding a guessed date; prompts are the unenforced
# half). Enforced HERE — the boundary every writer crosses. Deliberately NARROW: only the
# record-keeping fields — domain dates (a KEV due_date, an event date, a contract end) are
# legitimately future and are never checked. +1 day tolerance absorbs TZ skew (a UTC-thinking
# model just past midnight UTC is "tomorrow" relative to a US-eastern host clock).
_RECORD_DATE_FIELDS = ("published", "updated", "created", "last_updated")


def _future_date_reject(fm: dict, fields=_RECORD_DATE_FIELDS) -> Optional[str]:
    try:
        today = datetime.date.fromisoformat(_today())
    except ValueError:          # unparseable test override — never block writes on guard plumbing
        return None
    limit = today + datetime.timedelta(days=1)
    for k in fields:
        v = fm.get(k)
        d = None
        if isinstance(v, datetime.datetime):    # before date: datetime IS a date subclass
            d = v.date()
        elif isinstance(v, datetime.date):
            d = v
        elif isinstance(v, str):
            m = re.match(r"(\d{4}-\d{2}-\d{2})", v.strip())
            if m:
                try:
                    d = datetime.date.fromisoformat(m.group(1))
                except ValueError:
                    d = None
        if d and d > limit:
            return (f"{k}: {d.isoformat()} is in the future (today is {today.isoformat()}) — "
                    f"record-keeping dates must be the ACTUAL write date, never a guessed or "
                    f"future one; use today's date")
    return None


# --- briefing wikilink guard --------------------------------------------------------------
# Briefings are ANALYSIS pages that cite existing knowledge — every [[wikilink]] on one must
# resolve, or the flagship page a human reads daily ships dead links (live incident: the daily
# brief invented slugs from memory — [[entities/q/quimarat]] for the real quimat-rat page — and
# the broken-wikilinks drain's >=3-inbound wake gate treats 1-ref brief links as orphan noise
# forever). Scoped to briefings/ ONLY: source pages legitimately forward-reference entities that
# don't exist yet (the stub-creation drain depends on that), so a vault-wide check would break
# the ingest pattern. Rejection carries did-you-mean suggestions so the lane model can retry
# with the real slug — the same feedback loop schema rejections use.
_WIKILINK = re.compile(r"\[\[([^\]|#\n]+)")
_STRICT_LINK_NS = ("briefings",)


def _briefing_link_reject(p: Path, body: Optional[str]) -> Optional[str]:
    if _namespace(p) not in _STRICT_LINK_NS or not body:
        return None
    targets = []
    for m in _WIKILINK.finditer(body):
        t = m.group(1).strip().strip("/")
        if t.endswith(".md"):
            t = t[:-3]
        if t:
            targets.append(t)
    if not targets:
        return None
    # one walk builds both the exact rel-path set and the basename->rel-path map
    rels: set[str] = set()
    by_base: dict[str, str] = {}
    for f in WIKI.rglob("*.md"):
        rel = f.relative_to(WIKI).as_posix()[:-3]
        rels.add(rel)
        by_base.setdefault(f.stem, rel)
    broken = []
    for t in dict.fromkeys(targets):                      # de-dup, keep order
        if t in rels or ("/" not in t and t in by_base):
            continue
        base = t.split("/")[-1]
        if base in by_base:                               # right page, wrong dir/shard
            broken.append(f"[[{t}]] — did you mean [[{by_base[base]}]]?")
            continue
        near = difflib.get_close_matches(base, list(by_base), n=2, cutoff=0.6)
        hint = " — did you mean " + " or ".join(f"[[{by_base[n]}]]" for n in near) + "?" if near else ""
        broken.append(f"[[{t}]] (no such page){hint}")
    if broken:
        return ("briefing links must resolve to existing pages (cite what you actually read; "
                "do not guess slugs): " + "; ".join(broken))
    return None


def _create(path: str, frontmatter_yaml: Union[str, dict], body: str = "") -> str:
    p = _safe(path)
    if p is None:
        return "refused: path outside the vault wiki/"
    _wa = _wauth_refusal(path)
    if _wa:
        return _wa
    _rr = _reserved_refuse(p)
    if _rr:
        return _rr
    if p.exists():
        return f"refused: {_rel(p)} already exists — use update_entity"
    fm = _coerce_fm(frontmatter_yaml)
    if fm is None:
        return "rejected: frontmatter_yaml is not a valid YAML mapping"
    # Enforce the page lands in a schema-declared namespace (no stray-namespace fork, #115).
    nsr = _namespace_reject(p)
    if nsr:
        return f"rejected: {nsr}"
    fdr = _future_date_reject(fm)
    if fdr:
        return f"rejected: {fdr}"
    blr = _briefing_link_reject(p, body)
    if blr:
        return f"rejected: {blr}"
    # Identity-based dedup BEFORE writing: route a cosmetic duplicate to the
    # existing canonical (authority) or flag+refuse a slug collision, instead of
    # minting a second canonical (okengine#98/#99/#100). Stamps fm["id"].
    dedup = _dedup_on_create(path, p, fm, body)
    if dedup is not None:
        return dedup
    fm, drift = _normalize_drift(fm, p)            # converge on schema vocab (okengine#46)
    # Server stamps version/last_updated if absent, and an IMMUTABLE `created` on first write
    # (the OKF-envelope creation date = when the page was ingested; unlike last_updated it never
    # shifts on later edits, so "recent ingest" / age reporting is accurate).
    if "version" not in fm:
        fm["version"] = 1
    if "created" not in fm:
        fm["created"] = _now()
    if "last_updated" not in fm:
        fm["last_updated"] = _now()
    _stamp_maintainer(fm, creation=True)   # composition provenance (okengine#90 P3)
    # Provenance (okengine#132/#133): stamp the owning extension id when a networked
    # extension caller writes — the key disable/orphan/purge reads. Server-side, derived
    # from the scoped token, so a client can't spoof it. Stdio/admin writes get no stamp.
    _c = _caller()
    if _c.get("kind") == "extension" and _c.get("ext_id"):
        fm["extension_id"] = _c["ext_id"]
    # Ensure every page carries a human `name`. The ingest agent (esp. source ingest:
    # select_raw_batch -> agent -> okengine-write) puts the article title in the body's
    # `# H1` but doesn't always set a `name`/`title` field, leaving the page nameless in
    # the reader/backlinks/search. Derive `name` from the first true H1 when absent —
    # only when BOTH name and title are missing, so a curated name is never overridden,
    # and after id derivation, so the minted slug stays filename-based.
    if not str(fm.get("name") or fm.get("title") or "").strip():
        _h1 = _H1.search(body or "")
        if _h1:
            fm["name"] = _h1.group(1).strip()
    pol = _policy_reject(p, fm, "create")
    if pol:
        return f"rejected: {pol}"
    flags = drift + _review_flags(p, fm, prev=None)
    if flags:
        fm["needs_review"] = True
    content = _compose(fm, body)
    reason = schema_reject_reason(str(p), content)
    if reason:
        return f"rejected: {reason}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    # Write-synchronous id claim: the new page is now resolvable by id, so a later
    # cosmetic-variant write dedupes against it instead of forking a canonical.
    if _CONVERGE_OK and isinstance(fm.get("id"), str):
        try:
            _registry().by_id[fm["id"]] = _rel(p)
        except Exception:           # pragma: no cover - registry is best-effort
            pass
    ver = fm.get("version", 1)
    _append_log(f"- {_today()} mcp-write create {_rel(p)} v{ver}")
    note = _queue_review(p, flags)
    return f"created {_rel(p)} v{ver}{note}"


def _update(path: str, frontmatter_yaml: Union[str, dict, None] = None,
            body: Optional[str] = None) -> str:
    p = _safe(path)
    if p is None:
        return "refused: path outside the vault wiki/"
    _wa = _wauth_refusal(path)
    if _wa:
        return _wa
    _rr = _reserved_refuse(p)
    if _rr:
        return _rr
    if not p.is_file():
        return f"refused: {_rel(p)} does not exist — use create_entity"
    ferr = _frontmatter_error(p)
    if ferr:
        return f"rejected: {ferr} — repair the frontmatter before updating"
    cur_fm, cur_body = _read_page(p)
    new_fm = dict(cur_fm)
    if frontmatter_yaml is not None:
        patch = _coerce_fm(frontmatter_yaml)
        if patch is None:
            return "rejected: frontmatter_yaml is not a valid YAML mapping"
        # Future-date guard on ONLY the fields this patch supplies: a legacy page that already
        # carries a bad future date must stay fixable by an update that doesn't touch dates.
        fdr = _future_date_reject(patch, fields=tuple(k for k in _RECORD_DATE_FIELDS if k in patch))
        if fdr:
            return f"rejected: {fdr}"
        new_fm.update(patch)
    new_fm, drift = _normalize_drift(new_fm, p)    # converge on schema vocab (okengine#46)
    new_body = cur_body if body is None else body
    if body is not None:                           # only when this update REWRITES the body
        blr = _briefing_link_reject(p, new_body)
        if blr:
            return f"rejected: {blr}"
    # Bump version, stamp last_updated.
    try:
        new_fm["version"] = int(new_fm.get("version", 1)) + 1
    except (TypeError, ValueError):
        new_fm["version"] = 2
    new_fm["last_updated"] = _now()
    _stamp_maintainer(new_fm, creation=False)   # add this pack as a maintainer (okengine#90 P3)
    pol = _policy_reject(p, new_fm, "update", prev=cur_fm)
    if pol:
        return f"rejected: {pol}"  # existing file left untouched
    flags = drift + _review_flags(p, new_fm, prev=cur_fm)
    if flags:
        new_fm["needs_review"] = True
    content = _compose(new_fm, new_body)
    reason = schema_reject_reason(str(p), content)
    if reason:
        return f"rejected: {reason}"  # existing file left untouched
    p.write_text(content, encoding="utf-8")
    ver = new_fm["version"]
    _append_log(f"- {_today()} mcp-write update {_rel(p)} v{ver}")
    note = _queue_review(p, flags)
    return f"updated {_rel(p)} v{ver}{note}"


def _tombstone(path: str, reason: str, superseded_by: Optional[str] = None) -> str:
    p = _safe(path)
    if p is None:
        return "refused: path outside the vault wiki/"
    _wa = _wauth_refusal(path)
    if _wa:
        return _wa
    _rr = _reserved_refuse(p)
    if _rr:
        return _rr
    if not p.is_file():
        return f"refused: {_rel(p)} does not exist — nothing to tombstone"
    cur_fm, cur_body = _read_page(p)
    # a tombstone IS an update — it must clear the same namespace permission
    # matrix as every other mutation (found via okengine#166: an agent-read-only
    # lookup/ namespace could still be tombstoned through this one path)
    pol = _policy_reject(p, cur_fm, "update", prev=cur_fm)
    if pol:
        return f"rejected: {pol}"  # file left untouched
    new_fm = dict(cur_fm)
    new_fm["status"] = "tombstoned"
    new_fm["tombstone_reason"] = reason
    if superseded_by:
        new_fm["superseded_by"] = superseded_by
    try:
        new_fm["version"] = int(new_fm.get("version", 1)) + 1
    except (TypeError, ValueError):
        new_fm["version"] = 2
    new_fm["last_updated"] = _now()
    content = _compose(new_fm, cur_body)
    rej = schema_reject_reason(str(p), content)
    if rej:
        return f"rejected: {rej}"  # file left untouched
    p.write_text(content, encoding="utf-8")
    ver = new_fm["version"]
    _append_log(f"- {_today()} mcp-write tombstone {_rel(p)} v{ver} — {reason}")
    return f"tombstoned {_rel(p)} v{ver} (file retained, not deleted)"


def _flag(path: str, note: str) -> str:
    p = _safe(path)
    if p is None:
        return "refused: path outside the vault wiki/"
    _wa = _wauth_refusal(path)
    if _wa:
        return _wa
    clean_note = " ".join((note or "").split())
    wiki = _wiki()
    wiki.mkdir(parents=True, exist_ok=True)
    queue = wiki / "_review-queue.md"
    if not queue.exists():
        queue.write_text(
            "---\ntitle: Review Queue\n---\n\n"
            "# Review Queue\n\nAgent-flagged pages awaiting human review.\n\n",
            encoding="utf-8",
        )
    with queue.open("a", encoding="utf-8") as f:
        f.write(f"- {_today()} **{_rel(p)}** — {clean_note}\n")
    _append_log(f"- {_today()} mcp-write flag {_rel(p)} — {clean_note}")
    return f"flagged {_rel(p)} for review — queued in {_rel(queue)}"


# --- G1.1: body-preserving surgical edits + field-loss guard --------------
# update_entity does whole-body REPLACE (fine for frontmatter-only / wholesale
# rewrites); these add the SURGICAL primitives the drains need without resending
# the whole page: patch_entity (exact one-shot replace, like the Edit tool) and
# append_to_section (append into a `## heading` block). Both re-validate against
# schema, run the review gate, and — unlike the file tool — enforce a hard
# FIELD-LOSS guard: an edit may not drop an existing frontmatter key.

_STAMP_KEYS = {"version", "last_updated", "needs_review"}


def _field_loss(prev_fm: dict, new_fm: dict) -> Optional[str]:
    """Reject an edit that DROPS a frontmatter key present before (server-stamped
    keys excepted). Value changes and additions are fine — only deletions block."""
    dropped = sorted(k for k in (prev_fm or {})
                     if k not in (new_fm or {}) and k not in _STAMP_KEYS)
    if dropped:
        return ("edit would drop existing frontmatter field(s): "
                + ", ".join(dropped) + " — curated fields must be preserved")
    return None


def _stamp(new_fm: dict, cur_fm: dict) -> None:
    try:
        new_fm["version"] = int(new_fm.get("version", cur_fm.get("version", 1))) + 1
    except (TypeError, ValueError):
        new_fm["version"] = 2
    new_fm["last_updated"] = _now()


def _patch(path: str, old_string: str, new_string: str) -> str:
    p = _safe(path)
    if p is None:
        return "refused: path outside the vault wiki/"
    _wa = _wauth_refusal(path)
    if _wa:
        return _wa
    _rr = _reserved_refuse(p)
    if _rr:
        return _rr
    if not p.is_file():
        return f"refused: {_rel(p)} does not exist — use create_entity"
    if not old_string:
        return "rejected: old_string is empty"
    if old_string == new_string:
        return "rejected: old_string and new_string are identical"
    text = p.read_text(encoding="utf-8", errors="replace")
    n = text.count(old_string)
    if n == 0:
        return "rejected: old_string not found in the page (verify with a read first)"
    if n > 1:
        return f"rejected: old_string matches {n} places — add surrounding context to make it unique"
    cur_fm, _cur_body = _read_page(p)
    new_text = text.replace(old_string, new_string, 1)
    m = _FM.match(new_text)
    if not m:
        return "rejected: edit would remove or corrupt the YAML frontmatter"
    try:
        new_fm = yaml.safe_load(m.group(1)) or {}
    except Exception as e:
        return f"rejected: edit produced invalid frontmatter YAML: {str(e)[:120]}"
    if not isinstance(new_fm, dict):
        return "rejected: edit produced non-mapping frontmatter"
    fl = _field_loss(cur_fm, new_fm)
    if fl:
        return f"rejected: {fl}"
    pol = _policy_reject(p, new_fm, "update", prev=cur_fm)
    if pol:
        return f"rejected: {pol}"
    body = m.group(2)
    if body.startswith("\n"):
        body = body[1:]
    _stamp(new_fm, cur_fm)
    flags = _review_flags(p, new_fm, prev=cur_fm)
    if flags:
        new_fm["needs_review"] = True
    content = _compose(new_fm, body)
    rej = schema_reject_reason(str(p), content)
    if rej:
        return f"rejected: {rej}"  # file left untouched
    p.write_text(content, encoding="utf-8")
    ver = new_fm["version"]
    _append_log(f"- {_today()} mcp-write patch {_rel(p)} v{ver}")
    note = _queue_review(p, flags)
    return f"patched {_rel(p)} v{ver}{note}"


_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")


def _insert_into_section(body: str, heading: str, block: str) -> tuple[str, str]:
    """Append `block` at the end of the `## heading` section (matched by heading
    text, any level), before the next heading of the same-or-higher level. If the
    heading is absent, create the section at the end of the body."""
    lines = body.split("\n")
    want = heading.strip().lstrip("#").strip().lower()
    block = block.rstrip("\n")
    idx = level = None
    for i, ln in enumerate(lines):
        m = _HEADING_RE.match(ln)
        if m and m.group(2).strip().lower() == want:
            idx, level = i, len(m.group(1)); break
    if idx is None:
        base = body.rstrip("\n")
        prefix = (base + "\n\n") if base else ""
        return f"{prefix}## {heading}\n\n{block}\n", "section created"
    end = len(lines)
    for j in range(idx + 1, len(lines)):
        m = _HEADING_RE.match(lines[j])
        if m and len(m.group(1)) <= level:
            end = j; break
    seg = lines[:end]
    while len(seg) > idx + 1 and seg[-1].strip() == "":
        seg.pop()
    tail = lines[end:]
    new_lines = seg + ["", block] + (([""] + tail) if tail else [""])
    return "\n".join(new_lines), "appended to existing section"


def _append_section(path: str, heading: str, text: str) -> str:
    p = _safe(path)
    if p is None:
        return "refused: path outside the vault wiki/"
    _wa = _wauth_refusal(path)
    if _wa:
        return _wa
    _rr = _reserved_refuse(p)
    if _rr:
        return _rr
    if not p.is_file():
        return f"refused: {_rel(p)} does not exist — use create_entity"
    if not (text or "").strip():
        return "rejected: text is empty"
    cur_fm, cur_body = _read_page(p)
    new_body, where = _insert_into_section(cur_body, heading, text)
    new_fm = dict(cur_fm)
    pol = _policy_reject(p, new_fm, "update", prev=cur_fm)
    if pol:
        return f"rejected: {pol}"
    _stamp(new_fm, cur_fm)
    flags = _review_flags(p, new_fm, prev=cur_fm)
    if flags:
        new_fm["needs_review"] = True
    content = _compose(new_fm, new_body)
    rej = schema_reject_reason(str(p), content)
    if rej:
        return f"rejected: {rej}"
    p.write_text(content, encoding="utf-8")
    ver = new_fm["version"]
    _append_log(f"- {_today()} mcp-write append {_rel(p)} v{ver} ({heading})")
    note = _queue_review(p, flags)
    return f"appended to '{heading}' in {_rel(p)} v{ver} ({where}){note}"


# --- converge-on-write (P2): upsert by id, merge under page+field ownership ---

_registries: dict = {}


def _registry() -> "id_index.IdIndex":
    """Lazy in-process id->path index over the current vault, kept write-synchronous
    (updated on each converge write). Keyed by vault path so tests stay isolated."""
    vault = Path(os.environ.get("WIKI_PATH") or str(VAULT))
    k = str(vault)
    if k not in _registries:
        _registries[k] = id_index.build(vault)
    return _registries[k]


def _governing(p: Path) -> dict:
    """The governing schema for a PAGE — sub-domain aware (okengine#177). Resolve by the page's
    LOCATION (its wiki-relative dir, which merged_schema walks up to the nearest schema.yaml /
    composed artifact), NOT a bare namespace. Passing the bare namespace lost the sub-domain and
    always resolved the ROOT schema, so for a walk-up sub-domain page the id/type-authority/owner
    guards saw root while the permission/shape guards (governing_policy -> _find_schema, a page-path
    walk-up) saw the sub-domain — the split-brain. Now both resolve the same governing schema."""
    vault = Path(os.environ.get("WIKI_PATH") or str(VAULT))
    try:
        nsdir = p.parent.relative_to(_wiki()).as_posix()   # 'acme/entities' | 'entities' | 'entities/a'
    except ValueError:
        nsdir = ""                                          # page outside wiki/ — vault-root schema
    if nsdir == ".":
        nsdir = ""
    return schema_lib.merged_schema(vault, nsdir)


def _page_id_and_kind(fm: dict, schema: dict, namespace: str, stem: str) -> tuple[str, str]:
    """(id, kind) for an incoming page: honour an explicit valid `id`, else derive
    from the type's authority binding (authority id) or a minted slug."""
    ptype = str(fm.get("type") or "").strip()
    authority, id_field = schema_lib.type_id_authority(schema, ptype)
    explicit = fm.get("id")
    if isinstance(explicit, str) and id_lib.is_id(explicit.strip()):
        pid = explicit.strip()
        scope = id_lib.parse_id(pid)[0]
        kind = "authority" if (authority and scope == id_lib.normalize_key(authority)) else "slug"
        return pid, kind
    return id_lib.derive_id(authority=authority,
                            local_id=(fm.get(id_field) if authority else None),
                            minted_scope=namespace,
                            slug_source=id_lib.natural_key(fm, stem))


def _converge(path: str, frontmatter_yaml: Union[str, dict], body: str = "",
              pack: str = "", remove: str = "") -> str:
    """Upsert a page by id: merge into a live page that already carries the id
    (page+field ownership), else create + claim. (RFC composable-okpacks §5a.)"""
    pack = pack or _prov_pack()   # deployment-pinned provenance + field ownership (okengine#90 P3)
    if not _CONVERGE_OK:
        return "rejected: converge unavailable (id/schema libs not importable)"
    p = _safe(path)
    if p is None:
        return "refused: path outside the vault wiki/"
    _wa = _wauth_refusal(path)
    if _wa:
        return _wa
    _rr = _reserved_refuse(p)
    if _rr:
        return _rr
    fm = _coerce_fm(frontmatter_yaml)
    if fm is None:
        return "rejected: frontmatter_yaml is not a valid YAML mapping"
    namespace = _namespace(p)
    schema = _governing(p)          # sub-domain aware (okengine#177); namespace is the bare mint scope
    pid, kind = _page_id_and_kind(fm, schema, namespace, p.stem)
    if not pid:
        return "rejected: cannot determine page id"
    fm["id"] = pid
    reg = _registry()
    existing_rel = reg.resolve(pid)

    if existing_rel and reg.is_tombstoned(pid):
        return (f"refused: id {pid} is tombstoned — write to its successor, "
                "never resurrect a tombstoned id")

    if existing_rel:
        existing_path = _wiki() / existing_rel
        same = existing_path.exists() and existing_path.resolve() == p.resolve()
        if not same:
            if kind != "authority":
                _flag(path, f"slug id collision: {pid} already used by {existing_rel}")
                return (f"refused: slug id {pid} already used by {existing_rel} — "
                        "flagged for review (slug ids never auto-merge)")
            p = existing_path                       # authority id -> the canonical page
            _wa = _wauth_refusal(p)                 # re-authorize: the redirect can point OUTSIDE
            if _wa:                                 # the caller's declared scope (okengine#178)
                return _wa
        if p.is_file():
            cur_fm, cur_body = _read_page(p)
            ftype = str(cur_fm.get("type") or fm.get("type") or "").strip()
            owner = schema_lib.type_owner(schema, ftype)
            fos = schema_lib.field_owners(schema, ftype)
            rm = [s.strip() for s in (remove or "").split(",") if s.strip()]
            merged, dec = converge.merge_frontmatter(
                cur_fm, fm, owner_pack=owner, caller_pack=(pack or None),
                field_owners=fos, remove=rm)
            new_body = cur_body if not body else body
            _stamp(merged, cur_fm)
            if pack:
                merged["last_modified_by"] = pack
            # Converge is an agent write into an EXISTING page: apply the same
            # write-governance as update_entity, not a bypass (#21). HARD namespace
            # permission gate first (a human-authored namespace refuses the write,
            # leaving the page untouched)...
            pol = _policy_reject(p, merged, "update", prev=cur_fm)
            if pol:
                return f"rejected: {pol}"
            # ...then SOFT review flags (categorical confidence verdict, changed
            # review field) — flag, never block.
            review = _review_flags(p, merged, prev=cur_fm)
            if review:
                merged["needs_review"] = True
            content = _compose(merged, new_body)
            rej = schema_reject_reason(str(p), content)
            if rej:
                return f"rejected: {rej}"
            p.write_text(content, encoding="utf-8")
            reg.by_id[pid] = _rel(p)
            ver = merged.get("version")
            _append_log(f"- {_today()} mcp-write converge {_rel(p)} (id {pid}) v{ver}")
            flags = list(review)
            if dec.conflicts:
                flags += [f"field `{k}`: {pack or 'caller'} attempted {a!r}, owner value {c!r} kept"
                          for k, c, a in dec.conflicts]
            note = _queue_review(p, flags) if flags else ""
            return (f"converged into {_rel(p)} (id {pid}) v{ver}: "
                    f"+{len(dec.added)} added, ~{len(dec.updated)} updated, "
                    f"-{len(dec.removed)} removed, {len(dec.conflicts)} conflict(s){note}")

    # new id -> create + claim it in the registry (record the creating pack)
    if pack:
        fm.setdefault("maintained_by", [pack])
        fm.setdefault("discovered_by", pack)
    result = _create(path, fm, body)
    if result.startswith("created"):
        cp = _safe(path)
        if cp is not None:
            reg.by_id[pid] = _rel(cp)
    return result


# --- FastMCP wrappers (delegate to the plain helpers) --------------------

try:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("okengine-write")

    @mcp.tool()
    def create_entity(path: str, frontmatter_yaml: str, body: str = "") -> str:
        """Create a NEW wiki page. Refuses if the page already exists (use
        update_entity). Validates the composed page against the governing
        schema.yaml BEFORE writing; on reject, writes nothing. Stamps
        version:1 + last_updated if absent, then appends a log.md line."""
        return _create(path, frontmatter_yaml, body)

    @mcp.tool()
    def update_entity(path: str, frontmatter_yaml: str = "",
                      body: Optional[str] = None) -> str:
        """Update an EXISTING wiki page. Merges frontmatter keys (if given) and replaces the
        body when `body` is provided. Pass body="" to intentionally CLEAR the body; OMIT body
        (leave it null) to keep the current body. Bumps version, sets last_updated. Validates
        before writing; on reject the existing file is untouched."""
        fm = frontmatter_yaml if frontmatter_yaml else None
        return _update(path, fm, body)          # None -> keep body; "" -> clear; text -> replace

    @mcp.tool()
    def tombstone_entity(path: str, reason: str,
                         superseded_by: str = "") -> str:
        """Tombstone (NOT delete) an existing page: sets status: tombstoned,
        tombstone_reason, optional superseded_by, bumps version. The file is
        retained on disk. Validates before writing; appends a log.md line."""
        return _tombstone(path, reason, superseded_by or None)

    @mcp.tool()
    def flag_for_review(path: str, note: str) -> str:
        """Queue a page for human review by appending to wiki/_review-queue.md
        (created if absent). Does NOT mutate the target page. Logs the flag."""
        return _flag(path, note)

    @mcp.tool()
    def patch_entity(path: str, old_string: str, new_string: str) -> str:
        """Surgically edit ONE place in an existing page (like an exact-match
        find/replace) — body-preserving, no need to resend the whole page. Use for
        fixing a wikilink, inserting a section before a heading, changing one
        field. `old_string` must occur EXACTLY ONCE (add surrounding context to
        disambiguate). Rejects if the edit drops an existing frontmatter field,
        breaks the YAML, or violates schema. Bumps version, logs, review-gates."""
        return _patch(path, old_string, new_string)

    @mcp.tool()
    def append_to_section(path: str, heading: str, text: str) -> str:
        """Append `text` to the end of the `## heading` section of an existing page
        (heading matched by text, any level; created at end if absent). The safe
        primitive for append-only logs (## Evidence log, ## Recent activity) and
        adding a section (## Postmortem) — preserves all existing content. Bumps
        version, logs, review-gates."""
        return _append_section(path, heading, text)

    @mcp.tool()
    def converge_entity(path: str, frontmatter_yaml: str, body: str = "",
                        pack: str = "", remove: str = "") -> str:
        """Upsert a page by its IDENTITY (not its path). The id is taken from the
        frontmatter `id` or derived (an external-authority id when the type binds
        one, else a minted slug). If a LIVE page already carries this id, MERGE
        into it under page+field ownership: the owning pack may change any field; a
        non-owner may ADD new keys or change only fields it is granted; conflicts
        are flagged, never clobbered. If the id is new, create + claim it.
        Authority ids converge across packs; minted-slug collisions are flagged,
        never auto-merged. A write to a tombstoned id is refused (never resurrect).
        `pack` names the calling pack (for ownership; omit in single-pack use).
        `remove` is a comma-separated list of fields to drop — permitted only for
        fields the caller owns (a non-owner removal is flagged, not applied)."""
        return _converge(path, frontmatter_yaml, body, pack, remove)

except ImportError:  # pragma: no cover - mcp absent (e.g. host test env)
    mcp = None


class _ScopedWriteAuth:
    """ASGI middleware for the networked write surface (okengine#132): resolve
    `Bearer <token>` -> caller, 401 if unknown. The configured admin token
    (OKENGINE_MCP_TOKEN / OKENGINE_WRITE_TOKEN) keeps FULL write; an extension token
    from the vault store is limited to its write scopes. This surface is what lets an
    out-of-process sidecar reach okengine-write at all — stdio cannot."""

    def __init__(self, app, admin_token: str):
        self.app, self.admin_token = app, admin_token

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            headers = dict(scope.get("headers") or [])
            provided = headers.get(b"authorization", b"").decode()
            token = provided[7:] if provided.startswith("Bearer ") else ""
            caller = None
            if self.admin_token and hmac.compare_digest(token, self.admin_token):
                caller = {"kind": "admin", "write_scopes": None, "ext_id": None}
            else:
                rec = _scope.resolve(token)
                if rec is not None:
                    caller = {"kind": "extension", "ext_id": rec.get("ext_id"),
                              "write_scopes": rec.get("write_scopes") or []}
            if caller is None:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
            _caller_var.set(caller)
        await self.app(scope, receive, send)


if __name__ == "__main__":  # pragma: no cover
    if mcp is None:
        raise SystemExit("mcp package not installed; cannot run the server")
    transport = os.environ.get("OKENGINE_WRITE_TRANSPORT", "stdio")
    if transport in ("streamable-http", "http"):
        # Networked write surface for out-of-process sidecars. Requires a scoped or
        # admin token; refuses the built-in default unless explicitly allowed, and
        # fails closed off-loopback with the default (mirrors the read server).
        import uvicorn
        host = os.environ.get("OKENGINE_WRITE_HOST", "127.0.0.1")
        admin = (os.environ.get("OKENGINE_WRITE_TOKEN")
                 or os.environ.get("OKENGINE_MCP_TOKEN") or "")
        if not admin:
            raise SystemExit("okengine-write: networked transport requires "
                             "OKENGINE_WRITE_TOKEN (or OKENGINE_MCP_TOKEN) — refusing "
                             "to serve writes unauthenticated.")
        app = _ScopedWriteAuth(mcp.streamable_http_app(), admin)
        uvicorn.run(app, host=host, port=int(os.environ.get("OKENGINE_WRITE_PORT", "8731")))
    else:
        mcp.run(transport="stdio")
