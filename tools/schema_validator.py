"""OKF + domain schema validator.

Single source of truth for "is this file conformant?", read from a `schema.yaml`
discovered by walking up from the target file (like `.editorconfig`). Used by:

  - the write-time guard (`tools/file_operations.write_file` / `patch_replace`) —
    rejects non-conformant content before it ever lands, and
  - a pre-commit gate / drift-lint via the CLI: `python -m tools.schema_validator <files...>`.

Conformance = OKF v0.1 base (`type` required) + the per-type required fields
declared in `schema.yaml`.

TWO PROFILES (see the conformance spec, docs/okf/okengine-conformance-spec.md):

  - `schema_reject_reason()` — RUNTIME / fail-OPEN. Used by the write path + the
    file-tool write-guard. A missing/broken schema or a validator error PASSES, so
    an infra hiccup never bricks the agent's writes; only a real conformance
    violation rejects. No schema.yaml in the file's ancestry → off for that tree
    (this is what keeps the engine domain-agnostic — drop a schema.yaml in a vault
    root to turn enforcement on).
  - `conformance_reject_reason()` — STRICT / fail-CLOSED. For CI, release gates,
    and public conformance tests (CLI: `--strict`). A missing/unparsable schema or
    a validator error is itself a FAILURE, so a release can't pass on a check that
    was silently disabled. Genuinely out-of-scope files still pass.
"""
from __future__ import annotations

import fnmatch
import os
import re
import time
from pathlib import Path
from typing import Optional

try:
    import yaml
except Exception:  # pragma: no cover - yaml always present in the runtime venv
    yaml = None

_FM_RE = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*(?:\n|\Z)", re.S)
_SCHEMA_NAMES = ("schema.yaml", ".okf-schema.yaml")

# OKF v0.1 reserves these filenames as special/structural files exempt from the
# `type:` requirement (index = collection landing page, log = changelog,
# AGENTS = agent instructions, README = human intro). A schema.yaml may override
# via `reserved_files`. The engine ALSO generates root dashboards each cron run
# (HOT/HEALTH/BUNDLE.md) that are `type: dashboard` with no `id` — non-page
# artifacts that must be conformance-exempt on every surface. This set + the
# structural predicate below are the shared contract with
# okengine-mcp/write_server.py `_RESERVED_NAMES`/reserve-predicate and
# scripts/cron/conformance_audit._is_structural; tests/test_reserved_files_contract.py
# binds them so they cannot silently drift again (the okengine#193-class bug where
# the validator's narrow default left engine-generated HOT.md permanently non-conformant).
_OKF_RESERVED_DEFAULT = ("index.md", "log.md", "agents.md", "readme.md",
                         "hot.md", "health.md", "bundle.md")


def _is_generated_structural(basename: str) -> bool:
    """True for an engine-GENERATED structural file (not an authored page): the hierarchical
    index tree (INDEX.md and paginated INDEX-pNN.md), and any `_`/`.`-prefixed scaffold
    (_review-queue.md, .backlinks.json-adjacent md, …). Mirrors write_server's `startswith("index-p")`
    / `startswith("_")` reserve rule and conformance_audit._is_structural, so a page the write path
    treats as generated is never flagged non-conformant by the drift lint."""
    n = basename
    return (n.startswith("_") or n.startswith(".")
            or n == "INDEX.md" or n.startswith("INDEX-")
            or n.lower().startswith("index-p"))

# dir -> (cached_at_monotonic, schema path|None). Entries EXPIRE (okengine#49): a negative
# result cached forever would leave a long-running validator/write-server fail-open for a tree
# whose schema.yaml is added/moved later (until restart); a positive is also re-resolved if its
# file later vanished. TTL bounds staleness; the parsed content is cached separately by mtime.
_dir_to_schema: dict[str, tuple[float, Optional[str]]] = {}
_FIND_TTL = float(os.environ.get("OKENGINE_SCHEMA_FIND_TTL", "10") or 0)
_schema_cache: dict[str, tuple[float, dict]] = {}  # schema path -> (mtime, parsed)

# The engine-owned base schema (config/base-schema.yaml) is merged UNDER every
# pack schema: it supplies the universal `okf.required` floor (`type`) and the
# `okf.should` WARN tier (`id`). Resolved repo-relative; OKENGINE_BASE_SCHEMA
# overrides for deployed layouts. Absent → {} → behaviour identical to pre-base
# (fail-safe: a missing base never changes conformance verdicts).
_DEFAULT_BASE = Path(__file__).resolve().parents[1] / "config" / "base-schema.yaml"
_base_cache: dict[str, dict] = {}


def _base_schema() -> dict:
    if yaml is None:
        return {}
    p = str(os.environ.get("OKENGINE_BASE_SCHEMA") or _DEFAULT_BASE)
    if p not in _base_cache:
        try:
            data = yaml.safe_load(Path(p).read_text(encoding="utf-8"))
            _base_cache[p] = data if isinstance(data, dict) else {}
        except Exception:
            _base_cache[p] = {}
    return _base_cache[p]


def _base_merged(schema: dict) -> dict:
    """`schema` (the resolved pack schema.yaml OR the composed artifact) with the engine base
    schema merged UNDER it, for the keys the write gate enforces: `types`, `enums`, `field_enums`.

    The write path PREFERS <vault>/.okengine/composed-schema.yaml (base ⊕ pack ⊕ extensions) when
    present, but falls back to the RAW pack schema.yaml whenever no schema-bringing extension is
    enabled — and that raw schema carries NONE of the engine-universal governance in
    config/base-schema.yaml (the core-type `required` floors, the CLOSED tlp/source_kind/severity
    enums). Without this merge the enforced write boundary silently under-enforced base governance on
    any deployment lacking a composed artifact, and enforcement flipped on/off with any unrelated
    schema-bringing extension toggle. Mirrors scripts/cron/schema_lib._merge_base_pack so the write
    gate and the conformance audit agree on the governing schema. Re-merging base under the composed
    artifact is idempotent — the artifact already contains base and the resolved copy wins per key."""
    base = _base_schema()
    if not base:
        return schema
    eff = dict(schema)
    b_types, p_types = base.get("types") or {}, schema.get("types") or {}
    if b_types or p_types:
        eff["types"] = {**b_types, **p_types}            # pack/composed wins on a conflicting type
    b_en, p_en = base.get("enums") or {}, schema.get("enums") or {}
    if b_en or p_en:
        merged = {k: list(v) for k, v in b_en.items()}
        for k, vals in p_en.items():                     # a pack EXTENDS a base enum (adds values)
            cur = merged.get(k, [])
            merged[k] = cur + [v for v in (vals or []) if v not in cur]
        eff["enums"] = merged
    b_fe, p_fe = base.get("field_enums") or {}, schema.get("field_enums") or {}
    if b_fe or p_fe:
        eff["field_enums"] = {**b_fe, **p_fe}            # pack wins on a field->enum mapping
    return eff


def _find_schema(start: str) -> Optional[Path]:
    """Walk up from `start` to the first schema.yaml. Cached per directory."""
    p = Path(start)
    d = p if p.is_dir() else p.parent
    try:
        d = d.resolve()
    except OSError:
        return None
    key = str(d)
    cached = _dir_to_schema.get(key)
    if cached is not None:
        ts, sp = cached
        fresh = _FIND_TTL <= 0 or (time.monotonic() - ts) < _FIND_TTL
        # a stale negative must be re-walked (a schema may have appeared); a stale-or-removed
        # positive too (file vanished) — otherwise we'd return a dead path or stay fail-open.
        if fresh and (sp is None or Path(sp).is_file()):
            return Path(sp) if sp else None
    cur = d
    found: Optional[Path] = None
    while True:
        # Prefer the generated composed schema (engine ⊕ pack ⊕ enabled extensions,
        # okengine#90/#133) when present at a vault root — it is authoritative for the
        # whole tree, so extension-owned types validate. Absent => the pack schema.yaml
        # walk-up below (pre-#133 behavior).
        composed = cur / ".okengine" / "composed-schema.yaml"
        if composed.is_file():
            found = composed
            break
        for name in _SCHEMA_NAMES:
            cand = cur / name
            if cand.is_file():
                found = cand
                break
        if found or cur.parent == cur:
            break
        cur = cur.parent
    _dir_to_schema[key] = (time.monotonic(), str(found) if found else None)
    return found


def _load_schema(sp: Path) -> Optional[dict]:
    if yaml is None:
        return None
    try:
        mtime = sp.stat().st_mtime
    except OSError:
        return None
    hit = _schema_cache.get(str(sp))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = yaml.safe_load(sp.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    _schema_cache[str(sp)] = (mtime, data)
    return data


def _excluded(rel_posix: str, schema: dict) -> bool:
    base = rel_posix.rsplit("/", 1)[-1]
    for pat in schema.get("exclude") or []:
        pat = str(pat)
        if pat.endswith("/"):
            if rel_posix.startswith(pat) or rel_posix == pat[:-1]:
                return True
        elif fnmatch.fnmatch(rel_posix, pat) or fnmatch.fnmatch(base, pat):
            return True
    return False


def _present(fm: dict, key: str) -> bool:
    """A required field is satisfied if present, non-null, and (for scalars)
    non-empty. Empty lists pass the gate — drift-lint flags those — so a stub
    being filled in isn't rejected outright."""
    if key not in fm:
        return False
    v = fm[key]
    if v is None:
        return False
    if isinstance(v, str) and not v.strip():
        return False
    return True


def _field_values(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _enum_rule(schema: dict, typ: str, field: str):
    rules = schema.get("field_enums") or {}
    rule = rules.get(field)
    if not isinstance(rule, dict):
        return None
    by_type = rule.get("by_type")
    if isinstance(by_type, dict):
        type_rule = by_type.get(typ)
        if isinstance(type_rule, str):
            return {"enum": type_rule, "extensible": rule.get("extensible")}
        if isinstance(type_rule, dict):
            merged = dict(rule)
            merged.update(type_rule)
            return merged
    return rule if rule.get("enum") else None


def _enum_reject_reason(schema: dict, typ: str, fm: dict) -> Optional[str]:
    enums = schema.get("enums") or {}
    if not isinstance(enums, dict):
        return None
    for field in schema.get("field_enums") or {}:
        if field not in fm or fm[field] is None:
            continue
        rule = _enum_rule(schema, typ, field)
        if not rule or rule.get("extensible") is True:
            continue
        enum_name = rule.get("enum")
        allowed = enums.get(enum_name)
        if not isinstance(allowed, list):
            continue
        allowed_values = {str(v) for v in allowed}
        bad = [v for v in _field_values(fm[field]) if v not in allowed_values]
        if bad:
            allowed_fmt = ", ".join(sorted(allowed_values))
            return f"{field}={bad[0]!r} not in enum '{enum_name}' ({allowed_fmt})"
    return None


def _evaluate(abs_path: str, content: str) -> tuple[str, Optional[str]]:
    """Core conformance evaluation. Returns (kind, reason):

      ok     — conformant.
      skip   — OUT OF SCOPE for this schema (not .md, outside apply_under/root,
               excluded, or a reserved file). Passes in BOTH profiles.
      fail   — a real CONFORMANCE VIOLATION (reason set). Rejects in both profiles.
      error  — the governing schema or the validator is UNAVAILABLE/broken (reason
               set): no schema in ancestry, unparsable schema, PyYAML missing, or
               an internal validator exception. The runtime gate treats this as
               fail-OPEN (pass — never brick a write); the strict/conformance gate
               treats it as fail-CLOSED (reject — a release can't pass on a
               silently-disabled check).

    Never raises."""
    try:
        sp = _find_schema(abs_path)
        if sp is None:
            return ("error", "no governing schema.yaml found in the file's ancestry")
        schema = _load_schema(sp)
        if not schema:
            return ("error", f"governing schema is unparsable or empty: {sp}")
        # The apply-root is the dir the schema governs. A normal schema.yaml governs
        # its own dir; the generated composed schema lives in <vault>/.okengine/ but
        # governs the VAULT ROOT (its grandparent) — so pages under wiki/ resolve
        # relative to the vault, not to .okengine/ (okengine#133).
        root = sp.parent.parent if sp.parent.name == ".okengine" else sp.parent
        try:
            rel = Path(abs_path).resolve().relative_to(root)
        except (ValueError, OSError):
            return ("skip", None)                       # not under the schema root
        rel_posix = rel.as_posix()
        apply_under = schema.get("apply_under") or []
        if apply_under and not any(rel_posix.startswith(a) for a in apply_under):
            return ("skip", None)
        if not rel_posix.endswith(".md"):
            return ("skip", None)
        if _excluded(rel_posix, schema):
            return ("skip", None)
        # OKF reserved filenames + engine-generated structural files are exempt from the
        # `type:` contract (index.md/log.md/AGENTS.md/README.md, the regenerated root dashboards
        # HOT/HEALTH/BUNDLE.md, the INDEX tree, and any `_`/`.`-prefixed scaffold).
        bn = rel_posix.rsplit("/", 1)[-1]
        reserved = schema.get("reserved_files")
        reserved = tuple(str(r).lower() for r in reserved) if reserved else _OKF_RESERVED_DEFAULT
        if bn.lower() in reserved or _is_generated_structural(bn):
            return ("skip", None)

        m = _FM_RE.match(content)
        if not m:
            return ("fail", "missing YAML frontmatter (OKF requires at least a `type:` field)")
        if yaml is None:
            return ("error", "PyYAML unavailable — cannot validate frontmatter")
        try:
            fm = yaml.safe_load(m.group(1))
        except Exception as e:
            return ("fail", f"frontmatter is not valid YAML: {str(e)[:120]}")
        if not isinstance(fm, dict):
            return ("fail", "frontmatter is not a YAML mapping")

        # okf.required = the engine-base floor UNION the pack's (never loosens;
        # `type` is always present). The base guarantees `type` even if a pack
        # omits an `okf:` block.
        base_okf = (_base_schema().get("okf") or {})
        pack_okf = (schema.get("okf") or {})
        okf_req = sorted(set(base_okf.get("required") or ["type"]) |
                         set(pack_okf.get("required") or ["type"]))
        missing = [k for k in okf_req if not _present(fm, k)]
        if missing:
            return ("fail", f"missing required field(s): {', '.join(missing)}")

        # Governance is enforced against base ⊕ (resolved schema): the base-schema core types,
        # required floors and CLOSED enums must bind even when no composed artifact exists and the
        # raw pack omits them (else base governance silently toggles with unrelated extension state).
        eff = _base_merged(schema)
        t = str(fm.get("type") or "").strip()
        types = eff.get("types") or {}
        if t not in types:
            # strict_types is ENGINE-OWNED (base), not pack-settable — a pack
            # cannot loosen/tighten the global type taxonomy under composition.
            if _base_schema().get("strict_types"):
                return ("fail", f"unknown type '{t}' — not in schema.yaml taxonomy")
            return ("ok", None)
        req = types[t].get("required") or ["type"]
        miss = [k for k in req if not _present(fm, k)]
        if miss:
            return ("fail", f"type '{t}' is missing required field(s): {', '.join(miss)}")
        enum_reason = _enum_reject_reason(eff, t, fm)
        if enum_reason:
            return ("fail", enum_reason)
        return ("ok", None)
    except Exception as e:
        return ("error", f"validator error: {str(e)[:120]}")


def schema_reject_reason(abs_path: str, content: str) -> Optional[str]:
    """RUNTIME (fail-OPEN) gate — used by the write path. Returns a rejection
    reason only for an actual conformance violation; a missing/broken schema or a
    validator error passes (None) so a write is never bricked by infra. Out of
    scope and conformant both return None. Never raises."""
    kind, reason = _evaluate(abs_path, content)
    return reason if kind == "fail" else None


def conformance_reject_reason(abs_path: str, content: str) -> Optional[str]:
    """STRICT (fail-CLOSED) conformance gate — for CI / release / public
    conformance tests. Returns a reason for a conformance violation OR an
    unavailable/broken schema/validator, so a release can't pass on a check that
    was silently disabled. OUT-OF-SCOPE files (not .md, outside apply_under/root,
    reserved) still pass (None) — strict ≠ "everything must be a page". Never raises."""
    kind, reason = _evaluate(abs_path, content)
    return reason if kind in ("fail", "error") else None


def missing_should(abs_path: str, content: str) -> list[str]:
    """The WARN tier: engine-base `okf.should` fields absent from a page's
    frontmatter. Advisory only — NEVER rejects (kept out of `schema_reject_reason`
    so it can't block a write). Returns [] when there's no parseable frontmatter
    or no base `should` list. Used by drift-lint to flag e.g. a missing `id`
    before it's promoted to required."""
    try:
        should = (_base_schema().get("okf") or {}).get("should") or []
        if not should or yaml is None:
            return []
        m = _FM_RE.match(content)
        if not m:
            return []
        fm = yaml.safe_load(m.group(1))
        if not isinstance(fm, dict):
            return []
        return [k for k in should if not _present(fm, k)]
    except Exception:
        return []


def governing_policy(abs_path: str) -> dict:
    """Return the write-governance policy from the schema.yaml governing
    `abs_path` (walk-up, same discovery + cache as the conformance gate):

        {"permissions": {...}, "review": {...}}

    Empty dict for either block if unset / no schema / error. This is the
    pack-owned policy the *MCP write path* (okengine-mcp/write_server.py) reads —
    `permissions` are HARD structural rights (per-namespace create/update +
    delete:false→tombstone); `review` is SOFT (flag high-stakes assertions for
    human review, never block). Never raises (fail-open: the write path treats an
    empty policy as "no extra restrictions").
    """
    try:
        sp = _find_schema(abs_path)
        if sp is None:
            return {}
        schema = _load_schema(sp) or {}
        out = {}
        if isinstance(schema.get("permissions"), dict):
            out["permissions"] = schema["permissions"]
        if isinstance(schema.get("review"), dict):
            out["review"] = schema["review"]
        return out
    except Exception:
        return {}


def drift_policy(abs_path: str) -> dict:
    """Pack-declared field-drift normalization for the schema governing `abs_path`:

        {"field_aliases": {alias_key: canonical_key},
         "value_aliases": {field: {from_value: canonical_value}},
         "allowed":       {type: [field, ...]}}

    The MCP write path applies this so agent writes converge on the schema's vocabulary
    (e.g. `country`->`suspected_origin`, `CN`->`China`) instead of drifting (okengine#46).
    `allowed` (optional, per type) lets the write path FLAG unknown fields for review (G3,
    never block). Empty blocks when unset / no schema. Never raises (fail-open)."""
    try:
        sp = _find_schema(abs_path)
        if sp is None:
            return {}
        schema = _load_schema(sp) or {}
        out: dict = {}
        for key in ("field_aliases", "value_aliases", "allowed"):
            if isinstance(schema.get(key), dict):
                out[key] = schema[key]
        return out
    except Exception:
        return {}


def main(argv: list[str]) -> int:
    """CLI for the pre-commit gate / drift-lint: validate each path; exit 1 if
    any is non-conformant.

      --strict   use the fail-CLOSED conformance profile (a missing/broken
                 governing schema or validator error is a FAILURE, not a pass).
                 This is the CI / release / public-conformance gate; without it
                 the CLI uses the runtime fail-OPEN profile.
      --quiet    print only violations (suppress the should-warn advisories)."""
    strict = "--strict" in argv
    quiet = "--quiet" in argv
    args = [a for a in argv if not a.startswith("-")]
    check = conformance_reject_reason if strict else schema_reject_reason
    bad = 0
    for path in args:
        try:
            content = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"  ! {path}: cannot read ({e})")
            bad += 1
            continue
        rpath = str(Path(path).resolve())
        reason = check(rpath, content)
        if reason:
            print(f"  ✗ {path}: {reason}")
            bad += 1
        elif not quiet:
            sh = missing_should(rpath, content)
            if sh:   # WARN tier — advisory, does not fail the gate
                print(f"  · {path}: should-warn — missing {', '.join(sh)}")
    if bad:
        print(f"\n{bad} file(s) fail {'strict conformance' if strict else 'schema conformance'}.")
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
