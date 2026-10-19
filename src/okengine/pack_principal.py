"""The pack principal of an enforced write (okengine#1027).

Field and page ownership on the write path compares a page's owner with the pack the writer acts
for. That identity used to come only from ``OKENGINE_PACK`` in the write server's environment, and
the write servers never had it: Hermes starts a stdio MCP server with a filtered environment (a
small safe baseline plus the server's own ``env:`` map, ``tools/mcp_tool_config._build_safe_env``),
so the gateway's ``OKENGINE_PACK`` never reaches it, and no rendered ``env:`` map set it. Every
agent lane then wrote as nobody: a non-owner of its own pack's pages, and free to name any owner
through ``converge_entity(pack=...)``, because without the env the argument was the only identity.

The identity therefore comes from the deployment's own definition, which every writer can read:

* the DEPLOYMENT pack is the ``name`` in ``<vault>/pack.yaml``. ``OKENGINE_PACK`` is accepted only
  where no ``pack.yaml`` declares one (a test vault, a legacy layout), and a value that disagrees
  with ``pack.yaml`` is refused, never silently preferred;
* the LANE pack refines it for a composed deployment. The lane is the server-bound
  ``OKENGINE_WRITE_ACTOR=cron:<job>`` (config, never a tool argument), and the engine already
  names the pack that drives a composed lane (``<job>@<pack>``, ``<pack>:<job>``, or a job a
  co-installed pack recorded in ``<vault>/.okengine/installed-domains/<pack>.json`` under its
  ``<pack>-`` prefix). Such a lane acts for that pack; every other caller acts for the deployment
  pack. Extension lanes keep their own ``ext:<id>`` principal, which the convergence service
  resolves separately.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

import yaml
from yaml import YAMLError  # a bare name: Cosmic Ray cannot mutate a dotted except entry

PACK_ENV = "OKENGINE_PACK"
_CRON_ACTOR = "cron:"


def declared_pack(vault: Path) -> str:
    """The ``name`` declared by ``<vault>/pack.yaml``, or "" when there is none to read."""
    try:
        meta = yaml.safe_load((vault / "pack.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, YAMLError):
        return ""
    name = meta.get("name") if isinstance(meta, dict) else None
    return name.strip() if isinstance(name, str) else ""


def deployment_pack(vault: Path, env: Mapping[str, str]) -> str:
    """The deployment's pack: ``pack.yaml`` first, ``OKENGINE_PACK`` only where it declares none."""
    return declared_pack(vault) or str(env.get(PACK_ENV) or "").strip()


def startup_refusal(vault: Path, env: Mapping[str, str]) -> str | None:
    """Why a write server must not start, or None when it has one unambiguous pack principal."""
    declared = declared_pack(vault)
    pinned = str(env.get(PACK_ENV) or "").strip()
    if declared and pinned and declared != pinned:
        return (f"{PACK_ENV}={pinned} disagrees with the name {declared!r} in {vault / 'pack.yaml'}; "
                "a write server will not guess which pack it writes for")
    if not (declared or pinned):
        return (f"no pack principal: {vault / 'pack.yaml'} declares no name and {PACK_ENV} is "
                "unset, so every write would carry no pack identity")
    return None


def installed_domain_jobs(vault: Path) -> dict[str, set[str]]:
    """``{pack: its cron job names}`` for every pack co-installed into this deployment."""
    out: dict[str, set[str]] = {}
    for path in sorted((vault / ".okengine" / "installed-domains").glob("*.json")):  # glob-ok: flat manifest dir
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        pack = manifest.get("pack") if isinstance(manifest, dict) else None
        if isinstance(pack, str) and pack.strip():
            jobs = manifest.get("cron_jobs")
            out[pack.strip()] = {str(job) for job in jobs} if isinstance(jobs, (dict, list)) else set()
    return out


def lane_pack(vault: Path, actor: str, deployment: str) -> str:
    """The pack a write by ``actor`` acts for, never one this deployment does not hold.

    Only a server-bound cron lane is refined; every other caller acts for the deployment pack.
    A lane names its pack in one of the forms the engine mints: a composed engine-template
    instance ``<job>@<pack>`` and a composed domain job ``<pack>:<job>`` (cron_pack_split
    .merge_packs), or a co-installed job listed in its pack's manifest or carrying the ``<pack>-``
    prefix install-domain requires (longest pack wins). A named pack other than a co-installed
    one leaves the deployment pack in place, so an extension id (``<ext>:<operation>``) or a stray
    suffix can never mint a principal."""
    if not deployment or not actor.startswith(_CRON_ACTOR):
        return deployment
    job = actor[len(_CRON_ACTOR):]
    installed = installed_domain_jobs(vault)
    lane, _, driver = job.partition("@")
    if driver in installed:
        return driver
    named, separator, _ = lane.partition(":")
    if separator and named in installed:
        return named
    for pack, jobs in installed.items():
        if job in jobs:
            return pack
    prefixed = [pack for pack in installed if lane.startswith(pack + "-")]
    return max(prefixed, key=len) if prefixed else deployment
