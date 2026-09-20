"""Runtime configuration and surface-auth validation services."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Callable


def check_installed_domain_drift(pack: Path, report: Any) -> None:
    """Report divergence between composed host content and ownership snapshots."""
    base = pack / ".okengine" / "installed-domains"
    if not base.is_dir():
        return
    try:
        import importlib.util

        path = Path(__file__).resolve().parent / "composed_pack_state.py"
        spec = importlib.util.spec_from_file_location("composed_pack_state_validate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        drift = module.all_installed_drift(pack)
    except Exception as exc:
        report.warn("composed pack drift", f"detector failed: {exc}")
        return
    if drift:
        for item in drift:
            report.warn(
                "composed pack drift",
                item + "; refresh from the owning pack before deploy",
            )
    else:
        count = len(list(base.glob("*.json")))  # glob-ok: ownership manifest directory is flat
        report.ok("composed pack drift", f"{count} ownership manifest(s) match")


def check_composition_state(pack: Path, report: Any) -> None:
    """What is composed into this host, and on what terms: manifest drift plus every standing
    trust-exposure acceptance. They answer one question and belong on the report together."""
    check_installed_domain_drift(pack, report)
    check_trust_exposure_overrides(pack, report)


def check_trust_exposure_overrides(pack: Path, report: Any) -> None:
    """Report every standing trust-exposure acceptance on this deployment (okengine#813).

    An accepted exposure must not become an invisible one: the guest's content is still served at
    the host's trust, so the decision belongs on every validate run, not only in the install log
    that recorded it. An entry naming a pack that is not installed is stale and says so.
    """
    path = pack / ".okengine" / "coinstall-overrides.yaml"
    if not path.is_file():
        return
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        report.warn("trust-exposure override", f"unreadable {path.name}: {str(exc)[:120]}")
        return
    entries = (data.get("trust_exposure") or {}) if isinstance(data, dict) else {}
    if not isinstance(entries, dict) or not entries:
        return
    domains = pack / ".okengine" / "installed-domains"
    installed = {p.stem for p in domains.glob("*.json")} if domains.is_dir() else set()  # glob-ok: flat manifest dir
    for name, entry in sorted(entries.items()):
        entry = entry if isinstance(entry, dict) else {}
        where = (f"guest '{entry.get('guest_trust')}' served at host '{entry.get('host_trust')}' "
                 f"since {entry.get('accepted_at')}: {entry.get('reason')}")
        if installed and name not in installed:
            report.warn("trust-exposure override",
                        f"{name} is not an installed domain — STALE acceptance ({where})")
        else:
            report.warn("trust-exposure override", f"{name}: {where}")


def check_env(pack: Path, r: Any, *, required: bool = True) -> None:
    ex = pack / ".env.example"
    if not ex.is_file() and required:
        r.warn(".env.example", "missing — operators won't know which secrets to set")
    elif ex.is_file():
        txt = ex.read_text(encoding="utf-8", errors="replace")
        if not re.search(
            r"(ANTHROPIC_API_KEY|DEEPSEEK_API_KEY|OPENROUTER_API_KEY|GOOGLE_API_KEY)", txt
        ):
            r.warn(".env.example model key", "no model-provider key documented")
        else:
            r.ok(".env.example")
    env = pack / ".env"
    if env.is_file():
        # a real .env must never be committed
        tracked = subprocess.run(
            ["git", "-C", str(pack), "ls-files", "--error-unmatch", ".env"],
            capture_output=True,
            text=True,
        )
        if tracked.returncode == 0:
            r.fail(
                ".env not committed", ".env is git-TRACKED — secrets leak; gitignore + remove it"
            )
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


def _is_exposed(text: str, env: dict, loopback: set[str]) -> bool:
    """True when the stack is bound beyond localhost. Local-first: the host-port
    interface is OKENGINE_BIND (default 127.0.0.1); exposure is either a non-loopback
    OKENGINE_BIND in .env or a hardcoded non-loopback host in a ports mapping."""
    bind = (env.get("OKENGINE_BIND") or "").strip()
    if bind and bind not in loopback:
        return True
    return "0.0.0.0:" in text  # a pack that hardcoded a wide bind in ports


def check_gateway_env(pack: Path, r: Any, yaml_module: Any) -> None:
    """The gateway must receive the pack `.env` so model-provider keys
    (OPENROUTER_API_KEY, …) and delivery tokens reach Hermes — via `env_file` or an
    explicit model-key `environment:` entry. Without it the providers can't
    authenticate and no LLM cron runs (#22)."""
    compose = pack / "docker-compose.yml"
    if not compose.is_file() or yaml_module is None:
        return
    try:
        data = yaml_module.safe_load(compose.read_text(encoding="utf-8")) or {}
    except Exception:
        return  # a malformed compose is surfaced by check_surface_auth's text scan
    gw = (data.get("services") or {}).get("gateway")
    if not isinstance(gw, dict):
        return
    if gw.get("env_file"):
        r.ok("gateway .env passthrough", "env_file")
        return
    env = gw.get("environment") or []
    env_text = (
        " ".join(str(k) for k in env) if isinstance(env, dict) else " ".join(str(x) for x in env)
    )
    if re.search(r"(ANTHROPIC|DEEPSEEK|OPENROUTER|GOOGLE)_API_KEY", env_text):
        r.ok("gateway .env passthrough", "explicit model-key env")
        return
    r.fail(
        "gateway .env passthrough",
        "the gateway service has no `env_file` and passes no model API key — providers won't "
        "authenticate at runtime (no LLM crons / delivery). Add `env_file: [{path: .env, "
        "required: false}]` to the gateway service in docker-compose.yml",
    )


def check_vault_mount(pack: Path, r: Any, yaml_module: Any) -> None:
    """WIKI_PATH must be a vault root whose last segment is NOT `wiki`. The engine derives the
    page tree as `WIKI_PATH/wiki` (write_server.py, build_hot_set.py, …), so `WIKI_PATH=/opt/wiki`
    doubles to `/opt/wiki/wiki` and a stray relative write forks the vault → split-brain
    (okengine#110). The convention is `/opt/vault`; the skeleton + okpack-cti use it."""
    compose = pack / "docker-compose.yml"
    if not compose.is_file() or yaml_module is None:
        return
    try:
        data = yaml_module.safe_load(compose.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    paths = set()
    for svc in (data.get("services") or {}).values():
        if not isinstance(svc, dict):
            continue
        env = svc.get("environment") or []
        pairs = (
            env.items()
            if isinstance(env, dict)
            else (tuple(e.split("=", 1)) for e in env if isinstance(e, str) and "=" in e)
        )
        for k, v in pairs:
            if str(k).strip() == "WIKI_PATH":
                paths.add(str(v).strip().rstrip("/"))
    if not paths:
        r.ok("vault mount (WIKI_PATH)", "unset — defaults to /opt/vault")
        return
    bad = sorted(p for p in paths if p.rsplit("/", 1)[-1] == "wiki")
    if bad:
        r.fail(
            "vault mount (WIKI_PATH)",
            f"WIKI_PATH={bad[0]} ends in 'wiki' — the engine appends /wiki, so the page tree "
            f"doubles to {bad[0]}/wiki and relative writes fork the vault into a split-brain "
            f"(okengine#110). Mount the vault at /opt/vault and set WIKI_PATH=/opt/vault.",
        )
    elif len(paths) > 1:
        r.warn("vault mount (WIKI_PATH)", f"services disagree on WIKI_PATH: {sorted(paths)}")
    else:
        r.ok("vault mount (WIKI_PATH)", next(iter(paths)))


def check_surface_auth(
    pack: Path,
    r: Any,
    *,
    load_yaml: Callable[[Path], Any],
    read_dotenv: Callable[[Path], dict[str, str]],
    is_exposed: Callable[[str, dict[str, str]], bool],
) -> None:
    """Local-first guardrail (issues #20/#29): bound to localhost, the generic
    default MCP token is fine and the reader may stay open — a fresh scaffold
    passes. Once exposed beyond localhost, real secrets are REQUIRED (hard FAIL),
    so widening the bind forces the operator to set auth."""
    compose = pack / "docker-compose.yml"
    if not compose.is_file():
        return
    text = compose.read_text(encoding="utf-8", errors="replace")
    env = read_dotenv(pack)
    has_reader = "okengine-reader" in text and "ports:" in text
    has_cockpit = "okengine-cockpit" in text and "ports:" in text
    has_mcp = "okengine-mcp" in text and "ports:" in text
    if not is_exposed(text, env):
        r.info("network exposure", "host ports bind loopback (local-first default)")
        # False-confidence trap (okengine#208): the MCP CONTAINER binds 0.0.0.0 internally
        # (Dockerfile ENV OKENGINE_MCP_HOST=0.0.0.0 — Docker port-forwarding needs it), so
        # server.py's #50 fail-closed guard keys "exposed" on THAT, not the loopback HOST-port
        # mapping. With the built-in default token and no ALLOW_DEFAULT_TOKEN, the MCP SystemExits at
        # startup and crash-loops — even on a loopback deploy. deploy.sh avoids it (ensure-runtime
        # generates a secret token into .env); a bare `compose up` following .env.example does not.
        tok = (env.get("OKENGINE_MCP_TOKEN") or "").strip()
        allow = (env.get("OKENGINE_MCP_ALLOW_DEFAULT_TOKEN") or "").strip() == "1"
        if has_mcp and tok == "okengine-local" and not allow:
            r.warn(
                "MCP auth",
                "OKENGINE_MCP_TOKEN is the built-in default 'okengine-local' — the "
                "containerized MCP binds 0.0.0.0 and FAILS CLOSED at startup regardless of the "
                "loopback host-port mapping (#50/#208), so a bare `docker compose up` crash-loops "
                "it. Run deploy.sh (ensure-runtime generates a secret token), set a real "
                "OKENGINE_MCP_TOKEN, or OKENGINE_MCP_ALLOW_DEFAULT_TOKEN=1 for a throwaway stack.",
            )
        return
    r.warn(
        "network exposure", "OKENGINE_BIND exposes services beyond localhost — real auth required"
    )
    tok = (env.get("OKENGINE_MCP_TOKEN") or "").strip()
    if has_mcp and tok in ("", "okengine-local"):
        r.fail(
            "MCP auth",
            "exposed beyond localhost but OKENGINE_MCP_TOKEN is unset or the "
            "built-in default 'okengine-local' — set a real secret",
        )
    elif has_mcp:
        r.ok("MCP auth", "exposed with a custom token")
    # reader/cockpit auth is TRUST-AWARE (okengine#90 P4a): a PUBLIC reference deployment is
    # intentionally open, but a PRIVATE vault exposed without a password is a hard FAIL (both UIs
    # also fail-closes at runtime). They SHARE OKENGINE_READER_PASSWORD — the cockpit is a superset
    # of the reader and must not be laxer. Trust comes from pack.yaml.
    _trust = "private"
    if (pack / "pack.yaml").is_file():
        _trust = (
            str((load_yaml(pack / "pack.yaml") or {}).get("trust") or "private").strip().lower()
        )
    _has_pw = bool((env.get("OKENGINE_READER_PASSWORD") or "").strip())
    for _label, _present in (("reader auth", has_reader), ("cockpit auth", has_cockpit)):
        if not _present:
            continue
        if not _has_pw:
            if _trust == "public":
                r.warn(
                    _label,
                    "exposed with no password — intended for a PUBLIC pack (anyone can read)",
                )
            else:
                r.fail(
                    _label,
                    "PRIVATE pack exposed beyond localhost with no OKENGINE_READER_PASSWORD "
                    "— set a password, bind to loopback, or declare `trust: public` (#90 P4a)",
                )
        else:
            r.ok(_label, "exposed with a password set")


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


def check_runtime_config(
    pack: Path,
    r: Any,
    *,
    yaml_module: Any,
    model_profiles: Callable[[], Any],
    runtime_gitignored: Callable[[Path], bool],
) -> None:
    cfg = pack / ".hermes-data" / "config.yaml"
    if not cfg.is_file():
        # Context-aware: the runtime config is deploy-time state. In a definition
        # repo (.hermes-data gitignored) its absence is expected → WARN. Elsewhere
        # (a deploy-ready dir that should have seeded it) it's a FAIL.
        if runtime_gitignored(pack):
            r.info(
                ".hermes-data/config.yaml",
                "absent in definition checkout (gitignored runtime "
                "state); `framework init`/`pull` or `scripts/ensure-runtime.sh` must seed it "
                "before deployment",
            )
        else:
            r.fail(
                ".hermes-data/config.yaml",
                "missing — copy the engine config template and fill deployment keys",
            )
        return
    if yaml_module is None:
        r.warn(".hermes-data/config.yaml", "PyYAML unavailable; skipped parse")
        return
    try:
        data = yaml_module.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except Exception as e:
        r.fail(".hermes-data/config.yaml parses", f"YAML error: {str(e)[:140]}")
        return
    if not isinstance(data, dict):
        r.fail(".hermes-data/config.yaml shape", "top level is not a mapping")
        return
    # P0 cost containment: Hermes' fallback chain is global, so a paid/cloud
    # fallback remains reachable even when a lane explicitly selects @local.
    # The pack-level check sees the default here; deploy-cron-plus-jobs performs
    # the stronger post-expansion check across every generated job.
    mp = model_profiles()
    for error in mp.validate_qwen_no_fallback(data):
        r.fail("Qwen Coder fallback policy", error)
    missing = []
    if (data.get("terminal") or {}).get("backend") != "local":
        missing.append("terminal.backend: local")
    servers = data.get("mcp_servers") or {}
    if not isinstance(servers, dict) or "okengine" not in servers:
        missing.append("mcp_servers.okengine")
    if not isinstance(servers, dict) or "okengine-write" not in servers:
        missing.append("mcp_servers.okengine-write")
    scoped_writer_missing = (
        not isinstance(servers, dict) or "okengine-write-source-quality" not in servers
    )
    if missing:
        r.fail(".hermes-data/config.yaml required keys", ", ".join(missing))
    else:
        r.ok(".hermes-data/config.yaml")
    if scoped_writer_missing:
        r.warn(
            "mcp_servers.okengine-write-source-quality",
            "missing from an older runtime config; ensure-runtime.sh will add the "
            "server-bound job identity before containers are recreated",
        )
    else:
        r.ok("mcp_servers.okengine-write-source-quality", "server-bound job identity declared")
    # The seeded read-MCP Authorization header must be a real token, not the
    # template placeholder — an unsubstituted `<...>` 401s the gateway agent on
    # every read-MCP call (okengine#32).
    if isinstance(servers, dict):
        auth = ((servers.get("okengine") or {}).get("headers") or {}).get("Authorization") or ""
        if "<" in auth or "from pack .env" in auth:
            r.fail(
                "config.yaml okengine MCP auth",
                "Authorization still holds the template "
                "placeholder — re-seed via ensure-runtime.sh so the header matches the read "
                "server token (okengine#32)",
            )
        elif auth and not auth.startswith("Bearer "):
            r.warn("config.yaml okengine MCP auth", "Authorization is not a `Bearer <token>` value")
