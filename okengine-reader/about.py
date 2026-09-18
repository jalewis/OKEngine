"""Deployment identity service for the reader About panel."""
from __future__ import annotations

from pathlib import Path


def about_info(*, vault: Path, wiki: Path, env, yaml_module) -> dict:
    """Deployment identity for the About panel: the vault name + version (pack.yaml)
    and the engine/Hermes pins (engine.version). Read fresh — both files are tiny
    and About is cold."""
    info = {"vault": "", "vault_version": "", "ui_display_name": "vault reader",
            "engine_version": "", "hermes_pin": "", "project_url": "",
            # deployment timezone for the UI clock (okengine#301) — the container is started with
            # $TZ (compose passes TZ=${TZ:-UTC}); the clock renders in this zone, not UTC.
            "tz": env.get("TZ") or "UTC"}

    def _yaml(p: Path) -> dict:
        try:
            d = yaml_module.safe_load(p.read_text(encoding="utf-8")) if p.is_file() else None
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    pk = _yaml(vault / "pack.yaml")
    info["vault"] = str(pk.get("name") or "")
    info["vault_version"] = str(pk.get("version") or "")
    # Deployment identity is operator-owned and deliberately independent of pack identity.
    # Keep the former reader-specific variable as a compatibility alias for deployments that
    # adopted it before OKENGINE_UI_DISPLAY_NAME was introduced.
    info["ui_display_name"] = str(env.get("OKENGINE_UI_DISPLAY_NAME") or
                                  env.get("OKENGINE_READER_TITLE") or
                                  "vault reader").strip()
    # Deployment PURPOSE + composition (okengine#177-class ask: "what is this wiki,
    # what's installed?"). All DERIVED from state files that install-domain /
    # extensions-enable already maintain — nothing here is hand-written for About:
    #   description/mission  pack.yaml (declared; validate WARNs when absent)
    #   installed_domains    the '## Installed domain:' markers in the deployment
    #                        CLAUDE.md (the installer's provenance convention)
    #   sub_domains          walk-up subtrees (wiki/*/schema.yaml)
    #   extensions           enabled ids (.okengine/extensions.yaml)
    info["description"] = str(pk.get("description") or "")
    info["mission"] = str(pk.get("mission") or "")
    try:
        cm = (vault / "CLAUDE.md").read_text(encoding="utf-8") \
            if (vault / "CLAUDE.md").is_file() else ""
        info["installed_domains"] = [ln[len("## Installed domain:"):].strip()
                                     for ln in cm.splitlines()
                                     if ln.startswith("## Installed domain:")]
    except OSError:
        info["installed_domains"] = []
    try:
        info["sub_domains"] = sorted(d.name for d in wiki.iterdir()
                                     if d.is_dir() and (d / "schema.yaml").is_file())
    except OSError:
        info["sub_domains"] = []
    # Prefer the GENERATED effective set (opt-ins + core default-ons, written by
    # the deploy's stage-plan) — the enabled-state file lists opt-ins only, which
    # under-reported core extensions (a fleet running 3 showed 1 in About).
    eff = _yaml(vault / ".okengine" / "extensions-effective.yaml")
    if isinstance(eff.get("effective"), list) and eff["effective"]:
        # entries are {id,name,description} (or legacy plain ids) — normalize to dicts
        exts = []
        for x in eff["effective"]:
            if isinstance(x, dict):
                exts.append({"id": str(x.get("id") or ""),
                             "name": str(x.get("name") or x.get("id") or ""),
                             "description": str(x.get("description") or "")})
            else:
                exts.append({"id": str(x), "name": str(x), "description": ""})
        info["extensions"] = sorted(exts, key=lambda e: e["id"])
    else:
        ext = _yaml(vault / ".okengine" / "extensions.yaml")
        ids = sorted((ext.get("enabled") or {}).keys()) \
            if isinstance(ext.get("enabled"), dict) else []
        info["extensions"] = [{"id": i, "name": i, "description": ""} for i in ids]
    ev = _yaml(vault / "engine.version")
    # Prefer the deploy-stamped runtime marker (the ACTUAL engine/Hermes running, written by
    # ensure-runtime) over the pack's DECLARED engine.version pins, which can be stale/wrong vs
    # the deployed engine — a pack pinned to an older engine still deploys on a newer one, and
    # its hermes_pin then reports the wrong runtime (okengine#119). Fall back to the declared pin.
    rt = _yaml(vault / ".hermes-data" / "engine-runtime.yaml")
    info["engine_version"] = str(rt.get("engine_release") or ev.get("version") or "")
    info["hermes_pin"] = str(rt.get("hermes_pin") or ev.get("hermes_pin") or "")
    # The project/repo link for the About panel is deployment config — the engine ships no
    # hardcoded URL (stays publishable / no private host). Env wins; pack.yaml is fallback.
    info["project_url"] = env.get("OKENGINE_PROJECT_URL") or str(pk.get("project_url") or "")
    return info
