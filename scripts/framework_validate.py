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
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

from okengine import corpus_transaction
from scripts import framework_validate_crons
from scripts import framework_validate_docs
from scripts import framework_validate_extensions
from scripts import framework_validate_metadata
from scripts import framework_validate_models
from scripts import framework_validate_pack
from scripts import framework_validate_policy
from scripts import framework_validate_report
from scripts import framework_validate_runtime

try:
    import yaml
except Exception:
    yaml = None

# The engine's own cron scripts. Module-level so a test can point it elsewhere: the "engine cron
# dir is missing" branch must report UNDETECTABLE rather than a vacuous "no shadows", and a branch
# that cannot be reached from a test is a branch nobody has checked reports the right thing.
ENGINE_CRON_DIR = Path(__file__).resolve().parent / "cron"

_VER_RE = re.compile(r"\bv\d+\.\d+\.\d+\b")
# Unrendered scaffold placeholder, e.g. {{PACK}} / {{DOMAIN}}. Matches only
# {{UPPER_SNAKE}} (mirrors framework_init) so a Python f-string's {{...}} or a
# lowercase brace pair never trips it.
_TOKEN_RE = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")
# Declarative pack files where a surviving token = a broken deploy. (Cron *.py
# scripts are excluded — they're compile-checked, and f-strings use {{ }}.)
_TOKEN_SCAN = (
    "schema.yaml",
    "CLAUDE.md",
    "pack.yaml",
    "engine.version",
    "README.md",
    ".env.example",
    "docker-compose.yml",
    ".okengine/application.yaml",
    "crons/domain-crons.json",
    "crons/engine-template-prompts.json",
    ".hermes-data/config.yaml",
)
# scaffold placeholders that mean a field is still unfilled
_PLACEHOLDERS = (
    "<One line:",
    "<who reads",
    "<Steps the ingest",
    "<Schema +",
    "<Entity types",
    "Replace the placeholders",
)


Report = framework_validate_report.Report
def _load_yaml(p: Path):
    """Parse a YAML file; return None on a parse error so a broken pack file fails/skips gracefully
    instead of crashing the whole validator (callers report the FAIL or `or {}` past it)."""
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _pack_checks():
    return framework_validate_pack.PackChecks(
        engine_root=Path(__file__).resolve().parents[1],
        yaml_module=yaml,
        load_yaml=_load_yaml,
        engine_meta=_engine_meta_mod,
        version_re=_VER_RE,
        placeholders=_PLACEHOLDERS,
    )


def check_schema(pack: Path, r: Report) -> None:
    _pack_checks().check_schema(pack, r)


def _check_engine_inputs(sch: dict, type_names: set, r: Report) -> None:
    _pack_checks()._check_engine_inputs(sch, type_names, r)


def check_compose_drift(pack: Path, r: Report) -> None:
    _pack_checks().check_compose_drift(pack, r)


def check_prompt_residue(pack: Path, r: Report) -> None:
    _pack_checks().check_prompt_residue(pack, r)


def check_validator_vintage(pack: Path, r: Report) -> None:
    _pack_checks().check_validator_vintage(pack, r)


def check_subdomain_form(pack: Path, r: Report) -> None:
    _pack_checks().check_subdomain_form(pack, r)


def check_persona(pack: Path, r: Report) -> None:
    _pack_checks().check_persona(pack, r)


def _engine_meta_mod():
    """Load the sibling engine_meta module by path (no package assumptions)."""
    import importlib.util

    p = Path(__file__).resolve().parent / "engine_meta.py"
    spec = importlib.util.spec_from_file_location("engine_meta", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def check_engine_version(pack: Path, r: Report) -> None:
    _pack_checks().check_engine_version(pack, r)


def check_feeds(pack: Path, r: Report, probe: bool) -> None:
    _pack_checks().check_feeds(pack, r, probe)


def check_source_connectors(pack: Path, r: Report) -> None:
    _pack_checks().check_source_connectors(pack, r)


def _model_checks():
    return framework_validate_models.ModelChecks(model_profiles=_model_profiles_mod)


def _cron_expr(d: dict) -> str:
    return _model_checks()._cron_expr(d)


def _fixed_cron_hours(expr: str) -> list[int]:
    return _model_checks()._fixed_cron_hours(expr)


def _dst_schedule_problem(expr: str) -> str | None:
    return _model_checks()._dst_schedule_problem(expr)


def _model_profiles_mod():
    """Load the sibling model_profiles module by path (no package assumptions)."""
    import importlib.util

    p = Path(__file__).resolve().parent / "model_profiles.py"
    spec = importlib.util.spec_from_file_location("model_profiles", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def check_model_profiles(pack: Path, r: Report) -> None:
    _model_checks().check_model_profiles(pack, r)


def _collect_model_refs(pack: Path, mp) -> set[str]:
    return _model_checks()._collect_model_refs(pack, mp)


def check_crons(pack: Path, r: Report) -> None:
    framework_validate_crons.check_crons(
        pack,
        r,
        validator_dir=Path(__file__).resolve().parent,
        engine_cron_dir=ENGINE_CRON_DIR,
        load_yaml=_load_yaml,
        cron_expr=_cron_expr,
        dst_schedule_problem=_dst_schedule_problem,
    )


def check_installed_domain_drift(pack: Path, r: Report) -> None:
    framework_validate_runtime.check_installed_domain_drift(pack, r)


_LOOPBACK = ("127.0.0.1", "localhost", "::1")


check_env = framework_validate_runtime.check_env
_read_dotenv = framework_validate_runtime._read_dotenv


def _is_exposed(text: str, env: dict[str, str]) -> bool:
    return framework_validate_runtime._is_exposed(text, env, _LOOPBACK)


def check_gateway_env(pack: Path, r: Report) -> None:
    framework_validate_runtime.check_gateway_env(pack, r, yaml)


def check_vault_mount(pack: Path, r: Report) -> None:
    framework_validate_runtime.check_vault_mount(pack, r, yaml)


def check_surface_auth(pack: Path, r: Report) -> None:
    framework_validate_runtime.check_surface_auth(
        pack,
        r,
        load_yaml=_load_yaml,
        read_dotenv=_read_dotenv,
        is_exposed=_is_exposed,
    )


_runtime_gitignored = framework_validate_runtime._runtime_gitignored


def check_runtime_config(pack: Path, r: Report) -> None:
    framework_validate_runtime.check_runtime_config(
        pack,
        r,
        yaml_module=yaml,
        model_profiles=_model_profiles_mod,
        runtime_gitignored=_runtime_gitignored,
    )


def check_docs(pack: Path, r: Report) -> None:
    framework_validate_docs.check_docs(pack, r)


def check_wiki(pack: Path, r: Report) -> None:
    framework_validate_docs.check_wiki(pack, r)


def _pack_meta_mod():
    """Load the sibling pack_meta module by path (no package assumptions)."""
    import importlib.util

    p = Path(__file__).resolve().parent / "pack_meta.py"
    spec = importlib.util.spec_from_file_location("pack_meta", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _metadata_checks():
    return framework_validate_metadata.MetadataChecks(
        engine_root=Path(__file__).resolve().parents[1],
        load_yaml=_load_yaml,
        pack_meta=_pack_meta_mod,
    )


def check_pack_meta(pack: Path, r: Report) -> None:
    _metadata_checks().check_pack_meta(pack, r)


def check_owns_covers_schema(pack: Path, r: Report) -> None:
    _metadata_checks().check_owns_covers_schema(pack, r)


def _is_bundle(pack: Path) -> bool:
    return _metadata_checks()._is_bundle(pack)


def check_bundle(pack: Path, r: Report) -> None:
    _metadata_checks().check_bundle(pack, r)


def _discovery_mod():
    import importlib.util

    p = Path(__file__).resolve().parent / "extension_discovery.py"
    spec = importlib.util.spec_from_file_location("extension_discovery", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _extension_checks():
    return framework_validate_extensions.ExtensionChecks(
        load_yaml=_load_yaml,
        pack_meta=_pack_meta_mod,
        discovery=_discovery_mod,
    )


def _schema_ext_owners(pack: Path) -> set[str]:
    return _extension_checks()._schema_ext_owners(pack)


def check_extension_requirements(pack: Path, r: Report) -> None:
    _extension_checks().check_extension_requirements(pack, r)


def check_enabled_extensions_resolve(pack: Path, r: Report) -> None:
    _extension_checks().check_enabled_extensions_resolve(pack, r)


def check_inquiries(pack: Path, r: Report) -> None:
    _extension_checks().check_inquiries(pack, r)


def check_tokens(pack: Path, r: Report) -> None:
    """Unrendered scaffold placeholders ({{UPPER_SNAKE}}) mean a config/variable
    framework_init never filled — a guaranteed-broken deploy. Hard FAIL on any
    surviving token in a declarative pack file."""
    framework_validate_policy.check_tokens(pack, r, token_scan=_TOKEN_SCAN, token_re=_TOKEN_RE)


def check_application_profile(pack: Path, r: Report) -> None:
    """Validate an optional supported-application declaration.

    The application module owns the grammar and profile catalog. Keeping this adapter small makes
    ``framework validate`` the one author-facing conformance command instead of creating a second
    application CLI.
    """
    framework_validate_policy.check_application_profile(
        pack, r, engine_root=Path(__file__).resolve().parents[1], load_yaml=_load_yaml
    )


def check_policy_plane(pack: Path, r: Report) -> None:
    """Compose engine, pack, and extension policy before deployment."""
    framework_validate_policy.check_policy_plane(
        pack, r, engine_root=Path(__file__).resolve().parents[1],
        resolve_prompt=framework_validate_crons.prompt_text)

_pack_prompt_text = framework_validate_crons.prompt_text
def validate(pack: Path, probe: bool = False) -> Report:
    r = Report()
    check_tokens(pack, r)
    if _is_bundle(pack):
        # A bundle (okengine#181) owns nothing and ships no schema/persona/crons/feeds/wiki —
        # it composes other packs. Validate identity + recipe + engine pin + docs and the
        # tracked-secret guard; a non-runtime recipe has no environment to document. The
        # domain-content checks below don't apply and would spuriously FAIL on absent files.
        check_pack_meta(pack, r)
        check_bundle(pack, r)
        check_engine_version(pack, r)
        check_docs(pack, r)
        check_env(pack, r, required=False)
        return r
    check_schema(pack, r)
    check_persona(pack, r)
    check_engine_version(pack, r)
    check_pack_meta(pack, r)
    check_owns_covers_schema(pack, r)
    check_extension_requirements(pack, r)
    check_enabled_extensions_resolve(pack, r)
    check_inquiries(pack, r)
    check_application_profile(pack, r)
    check_policy_plane(pack, r)
    check_feeds(pack, r, probe)
    check_source_connectors(pack, r)
    check_crons(pack, r)
    framework_validate_runtime.check_composition_state(pack, r)
    check_model_profiles(pack, r)
    check_env(pack, r)
    check_gateway_env(pack, r)
    check_vault_mount(pack, r)
    check_surface_auth(pack, r)
    check_runtime_config(pack, r)
    check_docs(pack, r)
    check_wiki(pack, r)
    check_subdomain_form(pack, r)
    check_compose_drift(pack, r)
    check_prompt_residue(pack, r)
    check_validator_vintage(pack, r)
    return r


def main(argv: list[str]) -> int:
    return framework_validate_report.main(argv, validate, stable_corpus=corpus_transaction.stable_corpus)
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
