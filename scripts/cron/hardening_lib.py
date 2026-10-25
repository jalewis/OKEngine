#!/usr/bin/env python3
"""hardening_lib.py — the OKENGINE_HARDENED posture contract (okengine#78).

A single opt-in profile so an operator doesn't have to discover and set a dozen
independent safety flags. `OKENGINE_HARDENED=1` is an *assertion of intent*:
"this deployment must be safe to expose." It is **fail-closed** — the profile
never mints secrets or silently flips values; it names every unsafe setting and
the deployment-validation gate FAILs until the operator fixes each one.

`hardened_posture_violations(env)` is the single source of truth for what the
profile requires. It is PURE (no I/O, no os.environ) so both the runtime gate
(deployment_validate.check_auth, in-gateway) and any pre-deploy caller can share
one definition and it is unit-testable without a live stack.

The posture (each unsafe setting is one FAIL string):
  1. real MCP token       OKENGINE_MCP_TOKEN set and != the built-in loopback default
  2. reader auth OR public OKENGINE_READER_PASSWORD set, or OKENGINE_TRUST=public / a
                          deliberate OKENGINE_READER_PUBLIC=1 declaration
  3. rate limiting on      OKENGINE_READER_RATE not explicitly 0 (disabled)
  4. exports off if public a PUBLIC reader must not also enable OKENGINE_READER_EXPORTS
  5. UI editing off        OKENGINE_EDITING=0 — the reader Chat's write-back (okengine-write-chat)
                          must not be exposed on a hardened deployment (okengine#257)

Not re-checked here (already enforced UNCONDITIONALLY by deployment_validate.check_auth,
so hardened mode doesn't need to duplicate it): the Agent Chat (api_server) toolset
lockdown, and (okengine#983) `actorless_writer_bindings`, which keeps every model-facing
platform off the actor-less `okengine-write`. Those FAILs fire whether or not hardened mode is on.
"""
from __future__ import annotations

from typing import Any, Mapping

# Keep in sync with okengine-mcp/server.py DEFAULT_LOCAL_TOKEN — the built-in token a
# fresh deployment uses out of the box, safe ONLY because the host port binds to loopback.
DEFAULT_LOCAL_TOKEN = "okengine-local"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in _TRUTHY


def is_editing(env: Mapping) -> bool:
    """Whether UI editing is enabled — the reader Chat's write-back to the vault (via the
    okengine-write-chat MCP in the api_server toolset). DEFAULT ON for back-compat: editing is on
    unless OKENGINE_EDITING is an explicit falsey value (0/false/no/off). This is the operator
    switch; the REAL enforcement is ensure-runtime dropping okengine-write-chat from the
    api_server toolset when it's off (okengine#257)."""
    return str(env.get("OKENGINE_EDITING") or "").strip().lower() not in _FALSEY


def is_hardened(env: Mapping) -> bool:
    """Whether the deployment opted into the hardened posture (OKENGINE_HARDENED truthy)."""
    return _truthy(env.get("OKENGINE_HARDENED"))


def is_public(env: Mapping) -> bool:
    """A reader is 'public' if the pack trust is public OR the operator flipped the
    reader's public-mode flag. Public is a legitimate, deliberate posture — hardened
    mode allows it (no reader password required) but then requires exports stay off."""
    trust = str(env.get("OKENGINE_TRUST") or "private").strip().lower()
    return trust == "public" or _truthy(env.get("OKENGINE_READER_PUBLIC"))


def hardened_posture_violations(env: Mapping) -> list[str]:
    """Return one human-readable FAIL string per unsafe setting under the hardened
    posture. Empty list means either the profile is off (OKENGINE_HARDENED unset) or
    the posture is fully safe. Pure — pass a mapping (os.environ, a dict, a parsed
    .env); never reads the environment itself."""
    if not is_hardened(env):
        return []

    out: list[str] = []

    # 1. real MCP token — the built-in default is loopback-only.
    token = str(env.get("OKENGINE_MCP_TOKEN") or "").strip()
    if not token:
        out.append(
            "OKENGINE_MCP_TOKEN is unset — hardened mode requires a real MCP token "
            f"(the built-in '{DEFAULT_LOCAL_TOKEN}' is safe only on a loopback bind). "
            "Set OKENGINE_MCP_TOKEN to a generated secret.")
    elif token == DEFAULT_LOCAL_TOKEN:
        out.append(
            f"OKENGINE_MCP_TOKEN is still the built-in default '{DEFAULT_LOCAL_TOKEN}' — "
            "set a real generated token for a hardened deployment.")

    # 2. reader auth, OR a deliberate public declaration.
    public = is_public(env)
    password = str(env.get("OKENGINE_READER_PASSWORD") or "").strip()
    if not public and not password:
        out.append(
            "the reader has no auth and the vault is not declared public — hardened mode "
            "requires OKENGINE_READER_PASSWORD to be set, or OKENGINE_TRUST=public "
            "(an explicit, deliberate public declaration).")

    # 3. rate limiting must not be explicitly disabled.
    if str(env.get("OKENGINE_READER_RATE") or "").strip() == "0":
        out.append(
            "OKENGINE_READER_RATE=0 disables reader rate limiting — hardened mode requires "
            "it on. Unset it for the built-in default, or set a positive per-minute cap.")

    # 4. a public reader must not also enable exports (unbounded pandoc work).
    if public and _truthy(env.get("OKENGINE_READER_EXPORTS")):
        out.append(
            "OKENGINE_READER_EXPORTS is enabled on a PUBLIC reader — hardened mode requires "
            "exports off when public (the reader defaults them off for public; unset the flag).")

    # 5. UI editing (reader Chat write-back) must be OFF — a hardened/exposed deployment must
    # not let anonymous UI sessions write to the vault (okengine#257).
    if is_editing(env):
        out.append(
            "OKENGINE_EDITING is on — hardened mode requires UI editing OFF (the reader Chat "
            "writes back to the vault via the okengine-write-chat MCP). Set OKENGINE_EDITING=0; "
            "ensure-runtime then drops okengine-write-chat from the api_server toolset.")

    return out


# --- okengine#983: no model-facing platform binds the actor-less writer --------------------

#: The Agent Chat's actor-bound writer, and the actor it carries.
CHAT_WRITER = "okengine-write-chat"
CHAT_ACTOR = "chat:api_server"
#: The generic write server. It has no OKENGINE_WRITE_ACTOR, so the write path treats its caller
#: as admin: no capability, no namespace limit, no review.
ACTORLESS_WRITER = "okengine-write"
#: Platforms that are operator surfaces, not model-facing endpoints, each with the reason it is
#: exempt. Named here so the exemption is a deliberate, reviewable rule and never an omission.
OPERATOR_SURFACES = {
    "cli": "an interactive `hermes` session is reachable only via `docker exec` into the "
           "gateway, by an operator who already holds the container: admin-equivalent",
}


def actorless_writers(servers: Mapping) -> set[str]:
    """Every registered write server that carries no OKENGINE_WRITE_ACTOR."""
    out = set()
    for name, spec in servers.items():
        spec = spec if isinstance(spec, Mapping) else {}
        env = spec.get("env") if isinstance(spec.get("env"), Mapping) else {}
        runs_writer = any("write_server.py" in str(arg) for arg in (spec.get("args") or []))
        if (name == ACTORLESS_WRITER or runs_writer) and not str(
                env.get("OKENGINE_WRITE_ACTOR") or "").strip():
            out.add(str(name))
    return out


def _bound_mcp_servers(toolsets: Any, servers: set[str]) -> set[str]:
    """The MCP servers Hermes hands a platform for this `platform_toolsets` value.

    Mirrors hermes_cli.tools_config._merge_mcp_servers: `no_mcp` disables every server; a list
    that names registered servers is an allowlist of exactly those; any other list, and a missing
    or non-list value (Hermes then uses the platform default), binds EVERY registered server."""
    if not isinstance(toolsets, list):
        return set(servers)
    names = {str(t).strip() for t in toolsets}
    if "no_mcp" in names:
        return set()
    return (names & servers) or set(servers)


# --- okengine#991: platforms Hermes enables from environment credentials ---------------------

#: The Hermes tag ENV_ENABLED_PLATFORMS was read from. A test pins it to engine-manifest.yaml's
#: runtime.pinned_tag, so a Hermes bump fails until the table is re-verified against the new tag.
HERMES_ENV_TABLE_PIN = "v2026.9.14"

#: {platform: credential groups}. EVERY group must be present (a non-empty value); a tuple group is
#: satisfied by ANY of its names. Built-ins mirror the pinned Hermes gateway/config_env.py `_Cred`
#: rows in `_ENV_STEPS` exactly. The bundled plugin platforms (plugins/platforms/*: their
#: `_env_enablement` / `is_connected`) are approximated from their minimum credentials and lean to
#: over-report: a false hit costs the operator one explicit platform_toolsets entry, a miss leaves
#: an admin writer on a model-facing platform. api_server is not here: callers pass it as
#: `chat_enabled`. Config enablement in every spelling Hermes reads is config_enabled_platforms;
#: stored credentials (auth.json) are stored_enabled_platforms. Not modeled (over-reports, never
#: under-reports): GATEWAY_RELAY_URL disabling the direct platforms (config_env._relay; it can
#: only remove platforms). Not modeled (cannot be seen statically, or not read by these gates): a
#: third-party plugin platform registered at runtime, outside the bundled set; credentials Hermes
#: pulls from external secret sources (.op.env/1Password, Bitwarden) rather than a dotenv file; a
#: plugin platform's credentials written into its config block's `extra` (is_connected reads them);
#: and the legacy $HERMES_HOME/gateway.json base layer. Callers pass the dotenv view Hermes sees:
#: the pack .env overlaid by $HERMES_HOME/.env (= .hermes-data/.env).
ENV_ENABLED_PLATFORMS: dict[str, tuple] = {
    # gateway/config_env.py built-ins
    "telegram": ("TELEGRAM_BOT_TOKEN",),
    "discord": ("DISCORD_BOT_TOKEN",),
    "whatsapp_cloud": ("WHATSAPP_CLOUD_PHONE_NUMBER_ID", "WHATSAPP_CLOUD_ACCESS_TOKEN"),
    "slack": ("SLACK_BOT_TOKEN",),
    "signal": ("SIGNAL_HTTP_URL", "SIGNAL_ACCOUNT"),
    "mattermost": ("MATTERMOST_TOKEN",),
    "matrix": (("MATRIX_ACCESS_TOKEN", "MATRIX_PASSWORD"),),
    "homeassistant": ("HASS_TOKEN",),
    "email": ("EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_IMAP_HOST", "EMAIL_SMTP_HOST"),
    "sms": ("TWILIO_ACCOUNT_SID",),
    "dingtalk": ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"),
    "feishu": ("FEISHU_APP_ID", "FEISHU_APP_SECRET"),
    "wecom": ("WECOM_BOT_ID", "WECOM_SECRET"),
    "wecom_callback": ("WECOM_CALLBACK_CORP_ID", "WECOM_CALLBACK_CORP_SECRET"),
    "weixin": (("WEIXIN_TOKEN", "WEIXIN_ACCOUNT_ID"),),
    "bluebubbles": ("BLUEBUBBLES_SERVER_URL", "BLUEBUBBLES_PASSWORD"),
    "qqbot": (("QQ_APP_ID", "QQ_CLIENT_SECRET"),),
    "yuanbao": (("YUANBAO_APP_ID", "YUANBAO_APP_KEY"), "YUANBAO_APP_SECRET"),
    "relay": ("GATEWAY_RELAY_URL",),
    # bundled plugin platforms (plugins/platforms/*), minimum credentials
    "a2a": ("A2A_PORT",),
    "buzz": ("BUZZ_RELAY_URL",),
    "google_chat": (("GOOGLE_CHAT_HTTP_EVENTS_URL", "GOOGLE_CHAT_SUBSCRIPTION_NAME",
                     "GOOGLE_CHAT_SUBSCRIPTION"),),
    "irc": ("IRC_SERVER", "IRC_CHANNEL"),
    "line": ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET"),
    "ntfy": ("NTFY_TOPIC",),
    "photon": (("PHOTON_PROJECT_ID", "PHOTON_PROJECT_SECRET"),),
    "raft": ("RAFT_PROFILE",),
    "simplex": ("SIMPLEX_WS_URL",),
    "teams": ("TEAMS_CLIENT_ID", "TEAMS_CLIENT_SECRET", "TEAMS_TENANT_ID"),
}
#: Platforms Hermes enables on a truthy flag rather than a credential (config_env.py `_whatsapp`,
#: `_webhook`, `_msgraph_webhook`; truthy = Hermes utils.TRUTHY_STRINGS).
ENV_FLAG_PLATFORMS = {"whatsapp": "WHATSAPP_ENABLED", "webhook": "WEBHOOK_ENABLED",
                      "msgraph_webhook": "MSGRAPH_WEBHOOK_ENABLED"}


#: Every name pinned Hermes accepts as a platform (gateway/config.py: the Platform enum, plus the
#: bundled plugin platforms Platform._missing_ admits from plugins/platforms/*). Only these names are
#: platforms when spelled as `gateway.<x>` (config_loader._is_platform_name) or as a top-level `<x>:`
#: block (config_loader.shared_loop_targets); a third-party plugin registered at runtime is not
#: statically visible. `platforms.<x>` and `gateway.platforms.<x>` take any name.
HERMES_PLATFORM_NAMES = frozenset({
    # gateway/config.py Platform enum
    "local", "telegram", "discord", "whatsapp", "whatsapp_cloud", "slack", "signal", "mattermost",
    "matrix", "homeassistant", "email", "sms", "dingtalk", "api_server", "webhook",
    "msgraph_webhook", "feishu", "wecom", "wecom_callback", "weixin", "bluebubbles", "qqbot",
    "yuanbao", "relay",
    # plugins/platforms/* with __init__.py + plugin.yaml
    "a2a", "buzz", "google_chat", "irc", "line", "ntfy", "photon", "raft", "simplex", "teams",
})


def _hermes_bool(value: Any) -> bool:
    """PlatformConfig.from_dict's `_coerce_bool(enabled, False)`: None and any string Hermes does
    not recognize as truthy read as False; any other value by Python truthiness."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return bool(value)


def _gateway_section(cfg: Mapping) -> Mapping:
    gateway = cfg.get("gateway")
    return gateway if isinstance(gateway, Mapping) else {}


def _platform_blocks(cfg: Mapping, platform: str) -> tuple[list[Mapping], Mapping | None]:
    """(the nested blocks in Hermes' merge order, the top-level block) for one platform.

    config_loader.merge_platform_sections merges `gateway.platforms.<x>`, then `platforms.<x>`,
    then `gateway.<x>` (later wins on a shared key); bridge_platform_shared_keys then applies a
    top-level `<x>:` block's `enabled` over all three."""
    gateway = _gateway_section(cfg)
    sources = [gateway.get("platforms"), cfg.get("platforms")]
    nested = [src.get(platform) for src in sources if isinstance(src, Mapping)]
    top = None
    if platform in HERMES_PLATFORM_NAMES:
        nested.append(gateway.get(platform))
        # bridge_platform_shared_keys skips Platform.LOCAL's top-level block.
        top = cfg.get(platform) if platform != "local" else None
    return ([b for b in nested if isinstance(b, Mapping)],
            top if isinstance(top, Mapping) else None)


def platform_config_state(cfg: Mapping, platform: str) -> tuple[bool, bool]:
    """(enabled, explicit) as the pinned Hermes loader leaves `platform` before the env pass.

    `explicit` is Hermes' `_enabled_explicit` marker: ANY block carried an `enabled` key. An
    explicit platform that is not enabled stays off whatever credentials the environment or
    auth.json hold (config_env._enable_from_env, _enable_plugin_platform)."""
    nested, top = _platform_blocks(cfg, platform)
    enabled = explicit = False
    for block in nested + ([top] if top is not None else []):
        if "enabled" in block:
            enabled, explicit = _hermes_bool(block["enabled"]), True
    return enabled, explicit


def _explicitly_disabled(cfg: Mapping, platform: str) -> bool:
    """Some config block sets `enabled` and the merged value is off: Hermes then ignores the
    platform's env and stored credentials."""
    enabled, explicit = platform_config_state(cfg, platform)
    return explicit and not enabled


def _relay_url_configured(cfg: Mapping) -> bool:
    """A relay URL in config: `gateway.relay_url` (gateway/relay/__init__.py relay_url) or a relay
    block's `relay_url`, which PlatformConfig.from_dict lands in `extra` (config_env._relay)."""
    nested, top = _platform_blocks(cfg, "relay")
    urls = [_gateway_section(cfg).get("relay_url")]
    for block in nested + ([top] if top is not None else []):
        extra = block.get("extra")
        urls += [block.get("relay_url"), extra.get("relay_url") if isinstance(extra, Mapping)
                 else None]
    return any(str(url or "").strip() for url in urls)


def config_enabled_platforms(cfg: Mapping) -> list[str]:
    """Every platform the pinned Hermes enables from config.yaml alone, in any spelling it reads
    (platform_config_state), plus the relay when a relay URL is configured and not vetoed."""
    cfg = cfg if isinstance(cfg, Mapping) else {}
    gateway = _gateway_section(cfg)
    names: set[str] = set()
    for src in (gateway.get("platforms"), cfg.get("platforms")):
        if isinstance(src, Mapping):
            names.update(map(str, src))
    names.update(n for n in HERMES_PLATFORM_NAMES if n in cfg or n in gateway)
    out = {n for n in names if platform_config_state(cfg, n)[0]}
    if _relay_url_configured(cfg) and not _explicitly_disabled(cfg, "relay"):
        out.add("relay")
    return sorted(out)


#: Platforms that enable themselves from credentials Hermes stores in $HERMES_HOME/auth.json:
#: {platform: credential_pool key}. photon: plugins/platforms/photon/auth.py
#: load_project_credentials falls back to the first `credential_pool.photon_project` record
#: (spectrum_project_id or the older project_id, plus project_secret) when the env has none, and
#: adapter.is_connected / _env_enablement enable the platform on it. Leans to over-report like
#: the env table: any stored id or secret counts, since the env may supply the other half.
STORED_CREDENTIAL_PLATFORMS = {"photon": "photon_project"}
_STORED_CREDENTIAL_KEYS = ("spectrum_project_id", "project_id", "project_secret")


def read_auth_json(path) -> tuple[dict, str | None]:
    """($HERMES_HOME/auth.json as Hermes reads it, an error when it cannot be read here).

    The one I/O helper in this module. Hermes' photon _load_auth reads a missing file, and one
    that is not JSON, as {} (no stored credentials), so both are ({}, None) here too. A file that
    exists but cannot be read here (permissions, a directory) may still be readable by the
    gateway: that is ({}, error), and a caller must report it as undetectable, never as a pass."""
    from pathlib import Path
    import json

    path = Path(path)
    if not path.exists():
        return {}, None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return {}, f"{path} exists but cannot be read here ({type(exc).__name__})"
    try:
        data = json.loads(text) or {}
    except ValueError:
        return {}, None
    return (data if isinstance(data, dict) else {}), None


def stored_enabled_platforms(auth: Any, cfg: Mapping | None = None) -> dict[str, list[str]]:
    """{platform: [where its credentials are stored]} for every platform the pinned Hermes enables
    from `auth` (parsed $HERMES_HOME/auth.json), minus any `cfg` disables explicitly."""
    cfg = cfg if isinstance(cfg, Mapping) else {}
    pool = auth.get("credential_pool") if isinstance(auth, Mapping) else None
    out = {}
    for platform, key in STORED_CREDENTIAL_PLATFORMS.items():
        records = pool.get(key) if isinstance(pool, Mapping) else None
        record = records[0] if isinstance(records, list) and records else None
        if isinstance(record, Mapping) and any(record.get(k) for k in _STORED_CREDENTIAL_KEYS) \
                and not _explicitly_disabled(cfg, platform):
            out[platform] = [f"$HERMES_HOME/auth.json credential_pool.{key}"]
    return out


def env_enabled_platforms(env: Mapping, cfg: Mapping | None = None) -> dict[str, list[str]]:
    """{platform: [the env names that enable it]} for every platform the pinned Hermes enables
    from this environment, minus any `cfg` disables explicitly. Pure: pass os.environ in the
    gateway, or the parsed pack .env before the deploy."""
    cfg = cfg if isinstance(cfg, Mapping) else {}
    out: dict[str, list[str]] = {}
    for platform, groups in ENV_ENABLED_PLATFORMS.items():
        used = []
        for group in groups:
            names = group if isinstance(group, tuple) else (group,)
            present = [n for n in names if str(env.get(n) or "")]
            if not present:
                break
            used.extend(present)
        else:
            out[platform] = used
    for platform, flag in ENV_FLAG_PLATFORMS.items():
        if _truthy(env.get(flag)):
            out[platform] = [flag]
    return {p: out[p] for p in sorted(out) if not _explicitly_disabled(cfg, p)}


def platform_toolset_bindings(cfg: Mapping, *, chat_enabled: bool = False,
                              env: Mapping | None = None, auth: Any = None
                              ) -> dict[str, tuple[set[str], object]]:
    """{platform: (MCP servers Hermes binds it, its platform_toolsets value)} for every platform
    that has a platform_toolsets entry or that the pinned Hermes runs: enabled in config.yaml in
    any spelling (config_enabled_platforms), from environment credentials (env_enabled_platforms),
    from stored credentials (stored_enabled_platforms, `auth` = parsed auth.json), or the
    api_server when `chat_enabled`. A running platform with no entry binds EVERY server."""
    cfg = cfg if isinstance(cfg, Mapping) else {}
    servers = cfg.get("mcp_servers") if isinstance(cfg.get("mcp_servers"), Mapping) else {}
    toolsets = cfg.get("platform_toolsets")
    toolsets = toolsets if isinstance(toolsets, Mapping) else {}
    platforms = dict(toolsets)
    if chat_enabled:
        platforms.setdefault("api_server", None)
    for name in (*config_enabled_platforms(cfg), *env_enabled_platforms(env or {}, cfg),
                 *stored_enabled_platforms(auth, cfg)):
        platforms.setdefault(name, None)
    names = set(map(str, servers))
    return {str(p): (_bound_mcp_servers(platforms[p], names), platforms[p])
            for p in sorted(platforms, key=str)}


def actorless_writer_platforms(cfg: Mapping, *, chat_enabled: bool = False,
                               env: Mapping | None = None, auth: Any = None
                               ) -> dict[str, tuple[list[str], object]]:
    """{platform: (actor-less servers it can reach, its platform_toolsets value)} for every
    model-facing platform that can reach one. The structured form of actorless_writer_bindings,
    for a caller that must compare platforms rather than messages. `env` adds the platforms Hermes
    enables from environment credentials (env_enabled_platforms), `auth` those it enables from
    $HERMES_HOME/auth.json (stored_enabled_platforms)."""
    servers = cfg.get("mcp_servers") if isinstance(cfg.get("mcp_servers"), Mapping) else {}
    actorless = actorless_writers(servers)
    if not actorless:
        return {}
    out = {}
    for platform, (bound, toolset) in platform_toolset_bindings(
            cfg, chat_enabled=chat_enabled, env=env, auth=auth).items():
        if platform in OPERATOR_SURFACES:
            continue
        reach = sorted(bound & actorless)
        if reach:
            out[platform] = (reach, toolset)
    return out


def actorless_writer_bindings(cfg: Mapping, *, chat_enabled: bool = False,
                              env: Mapping | None = None, auth: Any = None) -> list[str]:
    """One FAIL string per model-facing platform that can reach an actor-less write server.

    Pure (no I/O): the pre-deploy `framework validate` and the in-gateway deployment check share
    it. A platform in OPERATOR_SURFACES is exempt by name. The api_server is model-facing whenever
    chat is enabled, even with no `platform_toolsets` entry, because Hermes then falls back to
    the platform default, which binds every MCP server."""
    out = []
    from_env = env_enabled_platforms(env or {}, cfg)
    stored = stored_enabled_platforms(auth, cfg)
    for platform, (bound, toolset) in actorless_writer_platforms(
            cfg, chat_enabled=chat_enabled, env=env, auth=auth).items():
        # None is both "no entry" (filled in by actorless_writer_platforms) and an explicit
        # null, which Hermes likewise treats as unset.
        how = ("has no platform_toolsets entry, so Hermes binds every MCP server"
               if toolset is None
               else f"platform_toolsets.{platform} = {toolset!r}")
        if platform in from_env:
            how += f"; enabled from the environment by {', '.join(from_env[platform])}"
        if platform in stored:
            how += f"; enabled by stored credentials in {', '.join(stored[platform])}"
        out.append(
            f"model-facing platform {platform!r} can reach the actor-less write server(s) "
            f"{bound} ({how}); the write path treats an actor-less caller as admin. "
            f"Give {platform} a platform_toolsets list without it (api_server: "
            f"[okengine, {CHAT_WRITER}], then re-run ensure-runtime; any other platform: "
            f"name the MCP servers it may use, e.g. okengine, or add no_mcp) (okengine#983).")
    return out
