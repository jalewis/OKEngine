#!/usr/bin/env python3
"""framework validate — pre-deploy sanity check for an OKF domain pack.

Catches the deploy-breaking mistakes before they hit production: a schema.yaml
that won't parse, a cron JSON with a bad shape, a cron script with a syntax
error, an unfilled persona, feeds that don't resolve, or a committed `.env`.
Domain-agnostic — validates the pack *spec* (docs/deploy-a-new-domain.md §1),
not any domain's content.

Usage:
  scripts/framework_validate.py <pack-dir> [--probe-feeds] [--quiet]

Exit: 0 = no FAILs (WARNs allowed) · 1 = at least one FAIL · 2 = bad invocation.

Severity is strict about real requirements:
  FAIL = a required file/config/variable is missing or wrong, so the deploy will
         error, a cron/lane won't run, or the pack ships incomplete (unrendered
         {{tokens}}, a missing/unpinned engine.version, a README that is missing,
         a stub, or has no Deploy section, a missing LICENSE, a cron with no
         usable schedule or no action, an empty engine-template prompt, an invalid
         pack.yaml enum, a gateway compose that never passes .env to the runtime,
         malformed schema/JSON, a committed .env, …).
  WARN = valid and deployable but worth fixing — inert-scaffold defaults (empty
         example feeds, unfilled persona placeholders), optional/engine-supplied
         fields, or a cross-pack type reference single-pack validate can't resolve.
  OK/INFO = fine.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except Exception:
    yaml = None

_VER_RE = re.compile(r"\bv\d+\.\d+\.\d+\b")
# Unrendered scaffold placeholder, e.g. {{PACK}} / {{DOMAIN}}. Matches only
# {{UPPER_SNAKE}} (mirrors framework_init) so a Python f-string's {{...}} or a
# lowercase brace pair never trips it.
_TOKEN_RE = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")
# Declarative pack files where a surviving token = a broken deploy. (Cron *.py
# scripts are excluded — they're compile-checked, and f-strings use {{ }}.)
_TOKEN_SCAN = ("schema.yaml", "CLAUDE.md", "pack.yaml", "engine.version",
               "README.md", ".env.example", "docker-compose.yml",
               "crons/domain-crons.json", "crons/engine-template-prompts.json",
               ".hermes-data/config.yaml")
# scaffold placeholders that mean a field is still unfilled
_PLACEHOLDERS = ("<One line:", "<who reads", "<Steps the ingest", "<Schema +",
                 "<Entity types", "Replace the placeholders")


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []   # (severity, check, detail)

    def add(self, sev: str, check: str, detail: str = ""):
        self.rows.append((sev, check, detail))

    def ok(self, c, d=""): self.add("OK", c, d)
    def info(self, c, d=""): self.add("INFO", c, d)
    def warn(self, c, d=""): self.add("WARN", c, d)
    def fail(self, c, d=""): self.add("FAIL", c, d)

    @property
    def n_fail(self): return sum(1 for s, _, _ in self.rows if s == "FAIL")
    @property
    def n_warn(self): return sum(1 for s, _, _ in self.rows if s == "WARN")


def _load_yaml(p: Path):
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def check_schema(pack: Path, r: Report) -> None:
    sp = pack / "schema.yaml"
    if not sp.is_file():
        r.fail("schema.yaml", "missing — the pack's contract is required")
        return
    if yaml is None:
        r.warn("schema.yaml", "PyYAML unavailable; skipped parse")
        return
    try:
        sch = _load_yaml(sp)
    except Exception as e:
        r.fail("schema.yaml parses", f"YAML error: {str(e)[:140]}")
        return
    if not isinstance(sch, dict):
        r.fail("schema.yaml shape", "top level is not a mapping")
        return
    r.ok("schema.yaml parses")
    okf = sch.get("okf") or {}
    req = okf.get("required") if isinstance(okf, dict) else None
    if not (isinstance(req, list) and "type" in req):
        r.warn("schema.okf.required", "should be a list containing `type` (OKF v0.1 base)")
    else:
        r.ok("schema.okf.required", f"{req}")
    types = sch.get("types")
    if not isinstance(types, dict) or not types:
        r.fail("schema.types", "must be a non-empty mapping of page types")
    else:
        bad = [t for t, d in types.items()
               if not isinstance(d, dict) or "type" not in (d.get("required") or ["type"])]
        if bad:
            r.warn("schema.types[].required", f"types whose required list omits `type`: {bad}")
        else:
            r.ok("schema.types", f"{len(types)} types")
    for block in ("partitioning", "hot_set"):
        (r.ok if isinstance(sch.get(block), dict) else r.warn)(
            f"schema.{block}", "" if isinstance(sch.get(block), dict) else "absent (engine defaults apply)")
    for block in ("permissions", "review", "tier"):
        if isinstance(sch.get(block), dict):
            r.info(f"schema.{block}", "declared (G2/G3/G4 policy)")
    if "strict_types" in sch:
        r.warn("schema.strict_types", "engine-owned (set in the engine base-schema) — a "
               "pack-level value is ignored; remove it")
    type_names = set(types) if isinstance(types, dict) else set()
    _check_engine_inputs(sch, type_names, r)


def _check_engine_inputs(sch: dict, type_names: set, r: Report) -> None:
    """The OPTIONAL engine cron inputs (type_aliases, classify_hints,
    operational_types, classify_catchall, depth_critical_types, protected_fields).
    Absent ⇒ generic engine behaviour. When present, shape-check them and warn on
    references to undeclared types."""
    # dict-shaped: {alias|type: ...}
    aliases = sch.get("type_aliases")
    if aliases is not None:
        if not isinstance(aliases, dict):
            r.fail("schema.type_aliases", "must be a mapping {alias: canonical-type}")
        else:
            unknown = sorted({str(v) for v in aliases.values()} - type_names) if type_names else []
            shadow = sorted({str(k) for k in aliases} & type_names)
            if unknown:
                r.warn("schema.type_aliases", f"alias target(s) not in `types:` {unknown}")
            elif shadow:
                r.warn("schema.type_aliases", f"alias key(s) are themselves declared types: {shadow}")
            else:
                r.info("schema.type_aliases", f"{len(aliases)} alias(es)")
    hints = sch.get("classify_hints")
    if hints is not None:
        if not isinstance(hints, dict):
            r.fail("schema.classify_hints", "must be a mapping {canonical-type: [tags]}")
        else:
            unknown = sorted({str(k) for k in hints} - type_names) if type_names else []
            (r.warn if unknown else r.info)(
                "schema.classify_hints",
                f"key(s) not in `types:` {unknown}" if unknown else f"{len(hints)} hinted type(s)")
    # list-shaped; the type-referencing ones warn on undeclared entries
    for key, refs_types in (("operational_types", True), ("classify_catchall", True),
                            ("depth_critical_types", True), ("protected_fields", False)):
        val = sch.get(key)
        if val is None:
            continue
        if not isinstance(val, list):
            r.fail(f"schema.{key}", "must be a list")
            continue
        if refs_types and type_names:
            unknown = sorted({str(x) for x in val} - type_names)
            (r.warn if unknown else r.info)(
                f"schema.{key}", f"entr(ies) not in `types:` {unknown}" if unknown else f"{len(val)} entr(ies)")
        else:
            r.info(f"schema.{key}", f"{len(val)} entr(ies)")


def check_persona(pack: Path, r: Report) -> None:
    cp = pack / "CLAUDE.md"
    if not cp.is_file():
        r.fail("CLAUDE.md (persona)", "missing — cron agents read this at $WIKI_PATH/CLAUDE.md")
        return
    txt = cp.read_text(encoding="utf-8", errors="replace")
    if len(txt.strip()) < 80:
        r.fail("CLAUDE.md (persona)", "effectively empty")
        return
    unfilled = [ph for ph in _PLACEHOLDERS if ph in txt]
    if unfilled:
        r.warn("CLAUDE.md filled in", f"{len(unfilled)} scaffold placeholder(s) remain — fill before deploy")
    else:
        r.ok("CLAUDE.md (persona)")


def _engine_meta_mod():
    """Load the sibling engine_meta module by path (no package assumptions)."""
    import importlib.util
    p = Path(__file__).resolve().parent / "engine_meta.py"
    spec = importlib.util.spec_from_file_location("engine_meta", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def check_engine_version(pack: Path, r: Report) -> None:
    # Required: the pack must pin the engine release it targets, AND that pin must
    # match the engine running this validator — you validate (and deploy) against
    # one engine, so the pack must declare that one. This is the single coupling
    # that catches engine/pack drift (the okpack-sec engine-v0.1.0 case).
    ev = pack / "engine.version"
    if not ev.is_file():
        r.fail("engine.version", "missing — pin the engine release the pack targets (e.g. v0.2.0)")
        return
    raw = ev.read_text(encoding="utf-8", errors="replace")
    # engine.version is YAML (engine/version/hermes_pin); read the keys when we
    # can, else fall back to grabbing the first vX.Y.Z token.
    ver = hpin = ""
    if yaml is not None:
        try:
            data = yaml.safe_load(raw)
        except Exception:
            data = None
        if isinstance(data, dict):
            ver = str(data.get("version") or "").strip()
            hpin = str(data.get("hermes_pin") or "").strip()
    if not ver:
        m = _VER_RE.search(raw)
        ver = m.group(0) if m else ""
    if not _VER_RE.fullmatch(ver):
        r.fail("engine.version", f"no vX.Y.Z pin found (got '{ver or raw.strip()[:40]}') — set `version: vX.Y.Z`")
        return

    # Compare against the engine this validator belongs to (single source of truth:
    # engine-manifest.yaml). If unreadable, fall back to the format-only check.
    try:
        meta = _engine_meta_mod()
        target, htag = meta.engine_release(), meta.hermes_pin()
    except Exception:
        target = htag = None
    if not target:
        r.ok("engine.version", ver)
        return
    if ver != target:
        r.fail("engine.version",
               f"pins {ver} but this engine is {target} — re-validate against {target} "
               f"and bump engine.version (or check out the matching engine)")
    else:
        r.ok("engine.version", f"{ver} (matches this engine)")
    if htag and hpin and hpin != htag:
        r.warn("engine.version hermes_pin",
               f"pins {hpin} but this engine targets Hermes {htag}")


def check_feeds(pack: Path, r: Report, probe: bool) -> None:
    fdir = pack / "feeds"
    opmls = sorted(fdir.glob("*.opml")) if fdir.is_dir() else []  # glob-ok: pack feeds/ is a flat dir, not a sharded content namespace
    if not opmls:
        r.warn("feeds/*.opml", "no OPML feed lists (pack may be query/enrichment-only)")
        return
    import xml.etree.ElementTree as ET
    urls: list[str] = []
    for f in opmls:
        try:
            tree = ET.parse(f)
        except Exception as e:
            r.fail(f"feeds/{f.name} parses", f"XML error: {str(e)[:120]}")
            continue
        found = [u for u in (el.attrib.get("xmlUrl", "").strip()
                             for el in tree.iter("outline")) if u]
        urls += found
        r.ok(f"feeds/{f.name}", f"{len(found)} feed url(s)")
    if not urls:
        names = ", ".join(f"feeds/{f.name}" for f in opmls)
        r.warn(names, "0 active feed URLs — pack is deployable but ingest stays idle until you add "
               "RSS/Atom <outline xmlUrl=…> entries (suggestions in feeds/*.example). Expected for a "
               "fresh inert pack; ignore until you enable ingest.")
        return
    if probe:
        import urllib.request
        dead = []
        for u in urls:
            try:
                req = urllib.request.Request(u, method="GET", headers={"User-Agent": "framework-validate/1"})
                with urllib.request.urlopen(req, timeout=12) as resp:
                    if resp.status >= 400:
                        dead.append(f"{u} ({resp.status})")
            except Exception as e:
                dead.append(f"{u} ({type(e).__name__})")
        if dead:
            r.warn("feeds reachable", f"{len(dead)}/{len(urls)} unreachable: " + "; ".join(dead[:5]))
        else:
            r.ok("feeds reachable", f"{len(urls)}/{len(urls)} live")
    else:
        r.info("feeds reachable", f"{len(urls)} url(s) not probed (pass --probe-feeds)")


def _cron_expr(d: dict) -> str:
    """Resolve a cron def's schedule expression across the supported shapes:
    {"schedule": {"expr": "..."}} | {"schedule": "..."} | {"expr": "..."}.
    Returns "" when no usable expression is present."""
    sched = d.get("schedule")
    if isinstance(sched, dict):
        return str(sched.get("expr") or "").strip()
    if isinstance(sched, str):
        return sched.strip()
    return str(d.get("expr") or "").strip()


def check_crons(pack: Path, r: Report) -> None:
    cdir = pack / "crons"
    if not cdir.is_dir():
        r.warn("crons/", "absent — pack contributes no cron defs")
        return
    dc = cdir / "domain-crons.json"
    defs = []
    if dc.is_file():
        try:
            defs = json.loads(dc.read_text(encoding="utf-8"))
            if not isinstance(defs, list):
                r.fail("crons/domain-crons.json shape", "must be a JSON array")
                defs = []
            else:
                r.ok("crons/domain-crons.json", f"{len(defs)} domain cron(s)")
        except Exception as e:
            r.fail("crons/domain-crons.json parses", f"JSON error: {str(e)[:120]}")
    else:
        r.info("crons/domain-crons.json", "absent (no domain crons)")
    etp = cdir / "engine-template-prompts.json"
    if etp.is_file():
        try:
            prompts = json.loads(etp.read_text(encoding="utf-8"))
            if not isinstance(prompts, dict):
                r.fail("crons/engine-template-prompts.json shape", "must be a JSON object")
            else:
                # An engine-template lane pairs an engine wake-gate script with a
                # pack-supplied prompt; an empty prompt = the agent wakes with no
                # instructions, so the lane is broken.
                empties = [k for k, v in prompts.items() if not str(v or "").strip()]
                (r.fail if empties else r.ok)(
                    "crons/engine-template-prompts.json",
                    f"empty prompt(s): {empties}" if empties else f"{len(prompts)} prompt(s)")
        except Exception as e:
            r.fail("crons/engine-template-prompts.json parses", f"JSON error: {str(e)[:120]}")
    # shape + script existence for domain defs
    sdir = cdir / "scripts"
    for d in defs:
        if not isinstance(d, dict) or not d.get("name"):
            r.fail("domain cron shape", f"entry missing name: {str(d)[:80]}")
            continue
        # A cron with no usable schedule expression can't be scheduled; one with
        # neither a script nor a prompt has nothing to run — both break the deploy.
        problems = []
        if not _cron_expr(d):
            problems.append("no usable schedule expr")
        if not (d.get("script") or d.get("prompt")):
            problems.append("no script or prompt")
        if problems:
            r.fail(f"cron '{d.get('name')}'", " + ".join(problems))
        scr = d.get("script") or ""
        if scr:
            base = Path(scr).name
            if not (sdir / base).is_file():
                r.warn(f"cron '{d.get('name')}' script", f"{base} not in crons/scripts/ (engine-provided?)")
    # syntax-check pack scripts in-process (compile() — no .pyc side effect, so a
    # read-only/foreign-owned scripts dir never yields a false positive).
    if sdir.is_dir():
        bad = []
        for py in sorted(sdir.glob("*.py")):  # glob-ok: pack scripts/ is a flat dir, not a sharded content namespace
            try:
                compile(py.read_text(encoding="utf-8", errors="replace"), str(py), "exec")
            except SyntaxError as e:
                bad.append(f"{py.name}:{e.lineno}")
            except OSError:
                pass  # unreadable file is not a syntax verdict
        (r.fail if bad else r.ok)("crons/scripts/*.py compile",
                                  f"syntax errors in: {bad}" if bad else "all parse")


def check_env(pack: Path, r: Report) -> None:
    ex = pack / ".env.example"
    if not ex.is_file():
        r.warn(".env.example", "missing — operators won't know which secrets to set")
    else:
        txt = ex.read_text(encoding="utf-8", errors="replace")
        if not re.search(r"(ANTHROPIC_API_KEY|DEEPSEEK_API_KEY|OPENROUTER_API_KEY|GOOGLE_API_KEY)", txt):
            r.warn(".env.example model key", "no model-provider key documented")
        else:
            r.ok(".env.example")
    env = pack / ".env"
    if env.is_file():
        # a real .env must never be committed
        tracked = subprocess.run(["git", "-C", str(pack), "ls-files", "--error-unmatch", ".env"],
                                 capture_output=True, text=True)
        if tracked.returncode == 0:
            r.fail(".env not committed", ".env is git-TRACKED — secrets leak; gitignore + remove it")
        else:
            r.info(".env", "present and untracked (ok)")


def _read_dotenv(pack: Path) -> dict[str, str]:
    env = pack / ".env"
    if not env.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in env.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _is_exposed(text: str, env: dict) -> bool:
    """True when the stack is bound beyond localhost. Local-first: the host-port
    interface is OKENGINE_BIND (default 127.0.0.1); exposure is either a non-loopback
    OKENGINE_BIND in .env or a hardcoded non-loopback host in a ports mapping."""
    bind = (env.get("OKENGINE_BIND") or "").strip()
    if bind and bind not in _LOOPBACK:
        return True
    return "0.0.0.0:" in text  # a pack that hardcoded a wide bind in ports


def check_gateway_env(pack: Path, r: Report) -> None:
    """The gateway must receive the pack `.env` so model-provider keys
    (OPENROUTER_API_KEY, …) and delivery tokens reach Hermes — via `env_file` or an
    explicit model-key `environment:` entry. Without it the providers can't
    authenticate and no LLM cron runs (#22)."""
    compose = pack / "docker-compose.yml"
    if not compose.is_file() or yaml is None:
        return
    try:
        data = yaml.safe_load(compose.read_text(encoding="utf-8")) or {}
    except Exception:
        return   # a malformed compose is surfaced by check_surface_auth's text scan
    gw = (data.get("services") or {}).get("gateway")
    if not isinstance(gw, dict):
        return
    if gw.get("env_file"):
        r.ok("gateway .env passthrough", "env_file")
        return
    env = gw.get("environment") or []
    env_text = " ".join(str(k) for k in env) if isinstance(env, dict) else " ".join(str(x) for x in env)
    if re.search(r"(ANTHROPIC|DEEPSEEK|OPENROUTER|GOOGLE)_API_KEY", env_text):
        r.ok("gateway .env passthrough", "explicit model-key env")
        return
    r.fail("gateway .env passthrough",
           "the gateway service has no `env_file` and passes no model API key — providers won't "
           "authenticate at runtime (no LLM crons / delivery). Add `env_file: [{path: .env, "
           "required: false}]` to the gateway service in docker-compose.yml")


def check_surface_auth(pack: Path, r: Report) -> None:
    """Local-first guardrail (issues #20/#29): bound to localhost, the generic
    default MCP token is fine and the reader may stay open — a fresh scaffold
    passes. Once exposed beyond localhost, real secrets are REQUIRED (hard FAIL),
    so widening the bind forces the operator to set auth."""
    compose = pack / "docker-compose.yml"
    if not compose.is_file():
        return
    text = compose.read_text(encoding="utf-8", errors="replace")
    env = _read_dotenv(pack)
    has_reader = "okengine-reader" in text and "ports:" in text
    has_mcp = "okengine-mcp" in text and "ports:" in text
    if not _is_exposed(text, env):
        r.info("network exposure", "host ports bind loopback (local-first default)")
        return
    r.warn("network exposure", "OKENGINE_BIND exposes services beyond localhost — real auth required")
    tok = (env.get("OKENGINE_MCP_TOKEN") or "").strip()
    if has_mcp and tok in ("", "okengine-local"):
        r.fail("MCP auth", "exposed beyond localhost but OKENGINE_MCP_TOKEN is unset or the "
               "built-in default 'okengine-local' — set a real secret")
    elif has_mcp:
        r.ok("MCP auth", "exposed with a custom token")
    if has_reader and not (env.get("OKENGINE_READER_PASSWORD") or "").strip():
        r.fail("reader auth", "exposed beyond localhost but .env has no OKENGINE_READER_PASSWORD")
    elif has_reader:
        r.ok("reader auth", "exposed with a password set")


def _runtime_gitignored(pack: Path) -> bool:
    """True when the pack's .gitignore excludes .hermes-data/ — i.e. this is a
    publishable *definition* repo where the runtime config is seeded at deploy,
    not committed (so a clone/CI checkout legitimately lacks config.yaml)."""
    gi = pack / ".gitignore"
    if not gi.is_file():
        return False
    for line in gi.read_text(encoding="utf-8", errors="replace").splitlines():
        if ".hermes-data" in line.split("#", 1)[0]:
            return True
    return False


def check_runtime_config(pack: Path, r: Report) -> None:
    cfg = pack / ".hermes-data" / "config.yaml"
    if not cfg.is_file():
        # Context-aware: the runtime config is deploy-time state. In a definition
        # repo (.hermes-data gitignored) its absence is expected → WARN. Elsewhere
        # (a deploy-ready dir that should have seeded it) it's a FAIL.
        if _runtime_gitignored(pack):
            r.warn(".hermes-data/config.yaml", "absent — runtime state (gitignored). Seed it before "
                   "`docker compose up` with `scripts/ensure-runtime.sh` (`framework init`/`pull` do "
                   "this automatically). Fine for a pack-definition checkout")
        else:
            r.fail(".hermes-data/config.yaml", "missing — copy the engine config template and fill deployment keys")
        return
    if yaml is None:
        r.warn(".hermes-data/config.yaml", "PyYAML unavailable; skipped parse")
        return
    try:
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except Exception as e:
        r.fail(".hermes-data/config.yaml parses", f"YAML error: {str(e)[:140]}")
        return
    if not isinstance(data, dict):
        r.fail(".hermes-data/config.yaml shape", "top level is not a mapping")
        return
    missing = []
    if ((data.get("terminal") or {}).get("backend") != "local"):
        missing.append("terminal.backend: local")
    servers = data.get("mcp_servers") or {}
    if not isinstance(servers, dict) or "okengine" not in servers:
        missing.append("mcp_servers.okengine")
    if not isinstance(servers, dict) or "okengine-write" not in servers:
        missing.append("mcp_servers.okengine-write")
    if missing:
        r.fail(".hermes-data/config.yaml required keys", ", ".join(missing))
    else:
        r.ok(".hermes-data/config.yaml")
    # The seeded read-MCP Authorization header must be a real token, not the
    # template placeholder — an unsubstituted `<...>` 401s the gateway agent on
    # every read-MCP call (okengine#32).
    if isinstance(servers, dict):
        auth = (((servers.get("okengine") or {}).get("headers") or {}).get("Authorization") or "")
        if "<" in auth or "from pack .env" in auth:
            r.fail("config.yaml okengine MCP auth", "Authorization still holds the template "
                   "placeholder — re-seed via ensure-runtime.sh so the header matches the read "
                   "server token (okengine#32)")
        elif auth and not auth.startswith("Bearer "):
            r.warn("config.yaml okengine MCP auth", "Authorization is not a `Bearer <token>` value")


def check_docs(pack: Path, r: Report) -> None:
    """A pack must document itself — the README an operator reads to deploy and
    understand it, plus a LICENSE. Missing README, a stub, no Deploy section, or
    a missing LICENSE is a FAIL; thin layout/structure coverage WARNs."""
    readme = pack / "README.md"
    if not readme.is_file():
        r.fail("README.md", "missing — a pack must ship a README (what it ingests, deploy, layout)")
        return
    txt = readme.read_text(encoding="utf-8", errors="replace")
    headings = [ln for ln in txt.splitlines() if ln.lstrip().startswith("## ")]
    if len(txt.strip()) < 200 or not headings:
        r.fail("README.md", "stub — document the pack (what it ingests, deploy, layout)")
        return
    # A Deploy section is MANDATORY — the operator must be told how to bring the
    # pack up. Match against heading text (a real section), not a prose mention.
    head_text = "\n".join(ln.lower() for ln in txt.splitlines() if ln.lstrip().startswith("#"))
    if not any(k in head_text for k in ("deploy", "install", "bring up", "quickstart", "getting started")):
        r.fail("README.md Deploy section",
               "no Deploy/Install/Quickstart heading — document how to bring the pack up")
    else:
        r.ok("README.md", f"{len(headings)} section(s)")
    low = txt.lower()
    if not any(k in low for k in ("layout", "services", "structure", "schema")):
        r.warn("README.md sections", "no layout/structure section found")
    # A pack must declare a license — required to publish/redistribute it.
    lic = next((pack / n for n in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING")
                if (pack / n).is_file()), None)
    if lic and lic.read_text(encoding="utf-8", errors="replace").strip():
        r.ok("LICENSE", lic.name)
    else:
        r.fail("LICENSE", "missing — every pack must ship a license (LICENSE / LICENSE.md / COPYING)")


def check_wiki(pack: Path, r: Report) -> None:
    w = pack / "wiki"
    if not w.is_dir():
        r.warn("wiki/", "absent — the content tree the engine compiles into")
        return
    r.ok("wiki/", "present")


def _pack_meta_mod():
    """Load the sibling pack_meta module by path (no package assumptions)."""
    import importlib.util
    p = Path(__file__).resolve().parent / "pack_meta.py"
    spec = importlib.util.spec_from_file_location("pack_meta", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def check_pack_meta(pack: Path, r: Report) -> None:
    """Shape-check the pack's pack.yaml (identity + composition metadata). Full
    cross-pack composition validation happens at deploy across all installed
    packs; here we only validate this pack's own declaration."""
    if not (pack / "pack.yaml").is_file():
        r.warn("pack.yaml", "absent — recommended for composition (name/version/owns/requires)")
        return
    try:
        meta = _pack_meta_mod().load_pack_meta(pack)
    except Exception as e:
        r.fail("pack.yaml", f"could not load: {str(e)[:120]}")
        return
    if meta is None:
        r.fail("pack.yaml parses", "present but unparseable / not a mapping")
        return
    if meta["trust"] not in ("public", "private"):
        r.fail("pack.yaml trust", f"'{meta['trust']}' — must be public or private")
    if not (meta["owns_types"] or meta["owns_namespaces"]):
        r.warn("pack.yaml owns", "declares no owned types/namespaces")
    else:
        r.ok("pack.yaml", f"{meta['name']} v{meta['version']} "
             f"({meta['trust']}; owns {len(meta['owns_types'])} type(s), "
             f"{len(meta['owns_namespaces'])} namespace(s))")
    if meta.get("port_offset"):
        r.info("pack.yaml port_offset", f"{meta['port_offset']} (reader {9200 + meta['port_offset']}, "
               f"mcp {8730 + meta['port_offset']} — applied by framework pull)")


def check_tokens(pack: Path, r: Report) -> None:
    """Unrendered scaffold placeholders ({{UPPER_SNAKE}}) mean a config/variable
    framework_init never filled — a guaranteed-broken deploy. Hard FAIL on any
    surviving token in a declarative pack file."""
    hits: list[str] = []
    for rel in _TOKEN_SCAN:
        p = pack / rel
        if not p.is_file():
            continue
        toks = sorted(set(_TOKEN_RE.findall(p.read_text(encoding="utf-8", errors="replace"))))
        if toks:
            hits.append(f"{rel}: {', '.join(toks[:6])}")
    if hits:
        r.fail("unrendered {{tokens}}", "; ".join(hits))
    else:
        r.ok("template tokens", "all rendered")


def validate(pack: Path, probe: bool = False) -> Report:
    r = Report()
    check_tokens(pack, r)
    check_schema(pack, r)
    check_persona(pack, r)
    check_engine_version(pack, r)
    check_pack_meta(pack, r)
    check_feeds(pack, r, probe)
    check_crons(pack, r)
    check_env(pack, r)
    check_gateway_env(pack, r)
    check_surface_auth(pack, r)
    check_runtime_config(pack, r)
    check_docs(pack, r)
    check_wiki(pack, r)
    return r


_GLYPH = {"OK": "✓", "INFO": "·", "WARN": "⚠", "FAIL": "✗"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Validate an OKF domain pack before deploy.")
    ap.add_argument("pack", help="path to the domain pack directory")
    ap.add_argument("--probe-feeds", action="store_true", help="HTTP-probe feed URLs (network)")
    ap.add_argument("--quiet", action="store_true", help="only print WARN/FAIL + summary")
    args = ap.parse_args(argv)
    pack = Path(args.pack).expanduser()
    if not pack.is_dir():
        print(f"ERROR: pack dir not found: {pack}", file=sys.stderr)
        return 2
    r = validate(pack, probe=args.probe_feeds)
    print(f"framework validate — {pack}\n")
    for sev, check, detail in r.rows:
        if args.quiet and sev in ("OK", "INFO"):
            continue
        line = f"  {_GLYPH[sev]} [{sev}] {check}"
        print(f"{line}: {detail}" if detail else line)
    verdict = "FAIL" if r.n_fail else ("PASS-with-warnings" if r.n_warn else "PASS")
    print(f"\n{verdict} — {r.n_fail} fail, {r.n_warn} warn, {len(r.rows)} checks")
    return 1 if r.n_fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
